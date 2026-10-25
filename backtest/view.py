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
    adx,
    atr,
    bollinger,
    closes,
    ema,
    highest,
    lowest,
    macd,
    percentile_rank,
    pivot_gap,
    pivot_gap_rank,
    pivots,
    realised_volatility,
    rsi,
    sma,
    volumes,
    vwap,
    zscore,
)
from backtest.resample import closes_at, resample
from marketdata.models import Bar, Interval

#: An indicator array's identity: its name, its period, and the one extra number
#: some of them take - MACD's slow period, a Bollinger band's width in deviations.
LineKey = tuple[str, int, float]


class Frame:
    """One timeframe's bars, and where in them we have got to.

    The cursor is the newest *closed* bar. On the timeframe being traded that is
    the bar whose close just produced a decision; on any higher timeframe it is
    the last one that finished before that moment.
    """

    def __init__(
        self,
        interval: Interval,
        bars: Sequence[Bar],
        lines: dict[LineKey, Line] | None = None,
    ) -> None:
        self.interval = interval
        self.bars = list(bars)
        self._cursor = -1
        #: Indicator arrays, filled on first use. Passed in when a study wants
        #: them kept for the next run over the same bars - see `Shared`.
        self._lines: dict[LineKey, Line] = lines if lines is not None else {}

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
        return self._at(("sma", length, 0.0), ago)

    def ema(self, length: int, ago: int = 0) -> float | None:
        return self._at(("ema", length, 0.0), ago)

    def rsi(self, length: int = 14, ago: int = 0) -> float | None:
        return self._at(("rsi", length, 0.0), ago)

    def atr(self, length: int = 14, ago: int = 0) -> float | None:
        return self._at(("atr", length, 0.0), ago)

    def pivot_gap(self, ago: int = 0) -> float | None:
        """The S1-R1 width, as a percentage of the pivot."""
        return self._at(("pivot_gap", 0, 0.0), ago)

    def pivot_gap_rank(self, length: int = 60, ago: int = 0) -> float | None:
        """Where that width stands among the last `length` periods, 0 to 100."""
        return self._at(("pivot_gap_rank", length, 0.0), ago)

    def line(self, name: str, length: int = 0, extra: float = 0.0, ago: int = 0) -> float | None:
        """Any indicator by name, for the ones without a method of their own.

        `extra` is the second number an indicator may take: MACD's slow period, a
        Bollinger band's width in standard deviations. The rest ignore it.
        """
        return self._at((name, length, extra), ago)

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

    def _at(self, key: LineKey, ago: int = 0) -> float | None:
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

    def _compute(self, key: LineKey) -> Line:
        name, length, extra = key
        if name == "atr":
            return atr(self.bars, length)
        if name == "pivot_gap":
            return pivot_gap(self.bars)
        if name == "pivot_gap_rank":
            return pivot_gap_rank(self.bars, length)
        if name == "vwap":
            return vwap(self.bars)
        if name in ("adx", "plus_di", "minus_di"):
            # One pass makes all three; the other two are filed so the next
            # operand to ask does not repeat it.
            trend, plus, minus = adx(self.bars, length)
            self._lines[("adx", length, extra)] = trend
            self._lines[("plus_di", length, extra)] = plus
            self._lines[("minus_di", length, extra)] = minus
            return self._lines[key]
        if name == "highest":
            return highest([b.high for b in self.bars], length)
        if name == "lowest":
            return lowest([b.low for b in self.bars], length)
        if name == "volume_sma":
            return sma(volumes(self.bars), length)
        # Not indicators a strategy can name - see `backtest/features.py`, which
        # reads them for the entry filter. They live here so that they are
        # computed once per series and read at the cursor like every other line.
        if name == "vol_z":
            return zscore(volumes(self.bars), length)
        if name == "rvol":
            return realised_volatility(closes(self.bars), length)
        if name == "rvol_rank":
            # How high the last 20 bars' volatility stands among the previous
            # `length` readings of it.
            return percentile_rank(realised_volatility(closes(self.bars), 20), length)
        prices = closes(self.bars)
        if name == "sma":
            return sma(prices, length)
        if name == "ema":
            return ema(prices, length)
        if name == "rsi":
            return rsi(prices, length)
        if name in ("macd", "macd_signal", "macd_hist"):
            line, signal, hist = macd(prices, length, int(extra))
            self._lines[("macd", length, extra)] = line
            self._lines[("macd_signal", length, extra)] = signal
            self._lines[("macd_hist", length, extra)] = hist
            return self._lines[key]
        if name in ("bb_upper", "bb_lower", "bb_width"):
            mid, upper, lower = bollinger(prices, length, extra)
            self._lines[("bb_upper", length, extra)] = upper
            self._lines[("bb_lower", length, extra)] = lower
            self._lines[("bb_width", length, extra)] = [
                (u - lo) / m * 100.0 if u is not None and lo is not None and m else None
                for u, lo, m in zip(upper, lower, mid, strict=True)
            ]
            return self._lines[key]
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


class Shared:
    """What a run works out that does not depend on the rule, kept for the next.

    A study runs hundreds of rules over the same bars. Resampling the hourly
    frame, aligning it to the five-minute bars and computing a 50-period EMA come
    out identically every time, so doing them once per process instead of once per
    trial is most of the saving between a trial that costs a second and one that
    costs a few hundred milliseconds.

    Valid only for one series: `bind` is called with the bars on every `build`,
    and a different series is an error rather than a stale answer.
    """

    def __init__(self) -> None:
        self._series: tuple[int, datetime, datetime] | None = None
        self._resampled: dict[Interval, list[Bar]] = {}
        self._lines: dict[Interval, dict[LineKey, Line]] = {}
        self._cursors: dict[tuple[Interval, ...], list[list[int]]] = {}

    def bind(self, bars: Sequence[Bar]) -> None:
        identity = (len(bars), bars[0].ts, bars[-1].ts) if bars else (0, _EPOCH, _EPOCH)
        if self._series is None:
            self._series = identity
        elif self._series != identity:
            raise ValueError("a Shared belongs to one series of bars")

    def lines(self, interval: Interval) -> dict[LineKey, Line]:
        return self._lines.setdefault(interval, {})

    def frame_bars(self, bars: Sequence[Bar], interval: Interval) -> list[Bar]:
        if interval not in self._resampled:
            self._resampled[interval] = resample(bars, interval)
        return self._resampled[interval]


_EPOCH = datetime(1970, 1, 1)


def build(
    bars: Sequence[Bar],
    base: Interval,
    context: Sequence[Interval] = (),
    shared: Shared | None = None,
) -> tuple[View, list[list[int]]]:
    """A view over these bars, and where each higher frame stands at each base bar.

    The alignment is computed once, here, rather than searched for on every bar:
    one walk forward per timeframe, because both series are in time order. The
    result is a list per base index of the cursor each context frame should be at.

    A context frame's cursor advances to the newest bar that had *closed* by the
    time the base bar closed. Not the bar containing that moment - the one before
    it, unless the two happen to end together.

    With `shared`, the resampled frames, their indicator arrays and the alignment
    are taken from it when it has them and put in it when it does not.
    """
    base_bars = list(bars)
    if shared is not None:
        shared.bind(base_bars)
    view = View(base=Frame(base, base_bars, shared.lines(base) if shared else None))
    for interval in context:
        if interval.seconds < base.seconds:
            raise ValueError(
                f"{interval} is shorter than the {base} being traded; "
                "a higher timeframe has to be higher"
            )
        higher = shared.frame_bars(base_bars, interval) if shared else resample(base_bars, interval)
        view.context[interval] = Frame(interval, higher, shared.lines(interval) if shared else None)

    intervals = list(view.context)
    if shared is not None and tuple(intervals) in shared._cursors:
        return view, shared._cursors[tuple(intervals)]

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

    if shared is not None:
        shared._cursors[tuple(intervals)] = cursors
    return view, cursors


def wind(view: View, cursors: Sequence[Sequence[int]], i: int) -> None:
    """Move every frame to base bar `i`."""
    view.base._cursor = i
    view.at = closes_at(view.base.bars[i].ts, view.base.interval)
    for slot, interval in enumerate(view.context):
        view.context[interval]._cursor = cursors[i][slot]
