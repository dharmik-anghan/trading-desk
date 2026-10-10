"""What a rule is allowed to know, at one instant.

A rule is never handed a list of bars. It is handed this, and this will not give
it a bar that has not finished.

That sounds like a formality until you write a rule that trades five-minute bars
with an hourly trend filter. At 10:07 the hourly bar covering 10:00 to 11:00 has
not closed. Hand a rule that bar - which any resample-and-index does - and it is
reading a price fifty-three minutes in its future, on every single bar, for the
whole run. The equity curve comes out beautiful and means nothing. It is the most
common bug in multi-timeframe backtesting and the hardest to notice, because
nothing about the output looks wrong.

So closedness is decided here, by comparing times, and a rule cannot opt out of
it. At 10:07 the newest hourly bar this will offer is the one that began at 09:00.

Indicators are read through a `Frame` rather than computed by the rule. The frame
holds one array per indicator for the whole series and returns the value at the
cursor, which is the same number the rule would get by computing over everything
up to now - that is exactly what `tests/analytics/test_indicators.py` asserts -
and it is computed once for the run instead of once per bar.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from analytics.indicators import (
    Line,
    Pivots,
    atr,
    closes,
    ema,
    pivot_gap,
    pivot_gap_rank,
    pivots,
    rsi,
    sma,
)
from backtest.resample import closes_at, resample
from marketdata.models import Bar, Interval


class Frame:
    """One timeframe's bars, and where in them we have got to.

    The cursor is the newest *closed* bar. On the timeframe being traded that is
    the bar whose close just produced a decision; on any higher timeframe it is
    the last one that finished before that moment.
    """

    def __init__(self, interval: Interval, bars: Sequence[Bar]) -> None:
        self.interval = interval
        self.bars = list(bars)
        self._cursor = -1
        self._lines: dict[tuple[str, int], Line] = {}

    # -- where we are ------------------------------------------------------

    @property
    def ready(self) -> bool:
        """False before this timeframe has produced its first closed bar.

        An hourly filter on a run that starts at 09:05 has nothing to say for
        fifty-five minutes, and a rule has to cope with that rather than be handed
        a bar that has not happened.
        """
        return self._cursor >= 0

    @property
    def bar(self) -> Bar | None:
        """The newest closed bar."""
        return self.bars[self._cursor] if self._cursor >= 0 else None

    @property
    def close(self) -> float | None:
        bar = self.bar
        return bar.close if bar else None

    def ago(self, n: int) -> Bar | None:
        """The bar `n` closed bars before the newest one. `ago(0)` is the newest."""
        index = self._cursor - n
        return self.bars[index] if 0 <= index <= self._cursor else None

    # -- indicators --------------------------------------------------------

    def sma(self, length: int, ago: int = 0) -> float | None:
        return self._at(("sma", length), ago)

    def ema(self, length: int, ago: int = 0) -> float | None:
        return self._at(("ema", length), ago)

    def rsi(self, length: int = 14, ago: int = 0) -> float | None:
        return self._at(("rsi", length), ago)

    def atr(self, length: int = 14, ago: int = 0) -> float | None:
        return self._at(("atr", length), ago)

    def pivot_gap(self, ago: int = 0) -> float | None:
        """The S1-R1 width, as a percentage of the pivot."""
        return self._at(("pivot_gap", 0), ago)

    def pivot_gap_rank(self, length: int = 60, ago: int = 0) -> float | None:
        """Where that width stands among the last `length` periods, 0 to 100."""
        return self._at(("pivot_gap_rank", length), ago)

    def price(self, field: str, ago: int = 0) -> float | None:
        """One of a bar's four prices, `ago` closed bars back."""
        bar = self.ago(ago)
        if bar is None:
            return None
        try:
            return float(getattr(bar, field))
        except AttributeError:
            raise KeyError(f"a bar has no {field!r}") from None

    def swing(self, long: bool, strength: int = 5, reach: int = 200) -> float | None:
        """The most recent confirmed swing low (for a long) or high (for a short).

        A swing low is a bar whose low is the lowest of `strength` bars either side.
        It can only be known `strength` bars after it happened, so the newest
        candidate is `strength` bars back - looking at a nearer bar would be using
        bars that have not been drawn yet. Ties go to the later bar.
        """
        newest = self._cursor - strength
        for j in range(newest, max(strength, newest - reach) - 1, -1):
            window = self.bars[j - strength : j + strength + 1]
            if long and self.bars[j].low <= min(b.low for b in window):
                return self.bars[j].low
            if not long and self.bars[j].high >= max(b.high for b in window):
                return self.bars[j].high
        return None

    def pivots(self, ago: int = 0) -> Pivots | None:
        """Levels for now, from the previous closed bar of this timeframe.

        Deliberately the bar before the newest one. Pivots are levels for a period
        derived from the period before it, so computing them from the bar the
        cursor is on would be using today's range to trade today.
        """
        previous = self.ago(ago + 1)
        return pivots(previous) if previous else None

    def _at(self, key: tuple[str, int], ago: int = 0) -> float | None:
        """This indicator `ago` closed bars back.

        `ago` is what makes a crossing expressible: "crossed above" is a claim
        about two bars, not one, and without a way to ask for the previous value
        a rule would have to keep its own state - which is exactly the state that
        stops the same rule running live.
        """
        index = self._cursor - ago
        if index < 0 or ago < 0:
            return None
        line = self._lines.get(key)
        if line is None:
            line = self._compute(key)
            self._lines[key] = line
        return line[index]

    def _compute(self, key: tuple[str, int]) -> Line:
        name, length = key
        if name == "atr":
            return atr(self.bars, length)
        if name == "pivot_gap":
            return pivot_gap(self.bars)
        if name == "pivot_gap_rank":
            return pivot_gap_rank(self.bars, length)
        prices = closes(self.bars)
        if name == "sma":
            return sma(prices, length)
        if name == "ema":
            return ema(prices, length)
        if name == "rsi":
            return rsi(prices, length)
        raise KeyError(f"no indicator called {name!r}")


@dataclass
class View:
    """Every timeframe, wound to the same instant."""

    base: Frame
    #: Higher timeframes, by interval. Built up front from the ones a rule asks
    #: for, so a rule cannot reach for one the engine has not aligned.
    context: dict[Interval, Frame] = field(default_factory=dict)
    at: datetime | None = None

    def frame(self, interval: Interval) -> Frame:
        if interval == self.base.interval:
            return self.base
        try:
            return self.context[interval]
        except KeyError:
            raise KeyError(
                f"{interval} was not asked for; a rule must declare its timeframes"
            ) from None


def build(
    bars: Sequence[Bar], base: Interval, context: Sequence[Interval] = ()
) -> tuple[View, list[list[int]]]:
    """A view over these bars, and where each higher frame stands at each base bar.

    The alignment is computed once, here, rather than searched for on every bar:
    one walk forward per timeframe, because both series are in time order. The
    result is a list per base index of the cursor each context frame should be at.

    A context frame's cursor advances to the newest bar that had *closed* by the
    time the base bar closed. Not the bar containing that moment - the one before
    it, unless the two happen to end together.
    """
    base_bars = list(bars)
    view = View(base=Frame(base, base_bars))
    for interval in context:
        if interval.seconds < base.seconds:
            raise ValueError(
                f"{interval} is shorter than the {base} being traded; "
                "a higher timeframe has to be higher"
            )
        view.context[interval] = Frame(interval, resample(base_bars, interval))

    intervals = list(view.context)
    cursors: list[list[int]] = []
    positions = [0] * len(intervals)

    for bar in base_bars:
        known_at = closes_at(bar.ts, base)
        row: list[int] = []
        for slot, interval in enumerate(intervals):
            frame = view.context[interval]
            index = positions[slot]
            # Advance while the *next* bar has also finished by now. The bar at
            # `index` is only usable if it has itself closed, which is what the
            # -1 below encodes: before that, this timeframe has nothing.
            while (
                index + 1 < len(frame.bars)
                and closes_at(frame.bars[index + 1].ts, interval) <= known_at
            ):
                index += 1
            positions[slot] = index
            usable = closes_at(frame.bars[index].ts, interval) <= known_at
            row.append(index if usable else -1)
        cursors.append(row)

    return view, cursors


def wind(view: View, cursors: Sequence[Sequence[int]], i: int) -> None:
    """Move every frame to base bar `i`."""
    view.base._cursor = i
    view.at = closes_at(view.base.bars[i].ts, view.base.interval)
    for slot, interval in enumerate(view.context):
        view.context[interval]._cursor = cursors[i][slot]
