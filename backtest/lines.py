"""Indicator lines for a chart, computed the way the engine reads them.

Shared by the backtest's trade chart and the trading desk's own chart, which is
the point: a line drawn by different code from the one that makes decisions can
be subtly wrong exactly where it matters, and two charts in the same app
disagreeing about where an EMA 20 sits is worse than either being wrong alone.

Everything goes through `View`, so a higher-timeframe line is the last value that
had *closed* - held flat until the next one does. On a backtest that is the
difference between honesty and a lie; on a live chart it is the difference
between what a rule will see and what the chart promised it would.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from analytics.indicators import Line
from backtest.view import Frame, build, wind
from marketdata.models import Bar, Interval

#: An indicator, its period, and the timeframe to read it on. None means the one
#: being charted.
Wanted = tuple[str, int, Interval | None]

#: Which indicators are prices and belong on the price axis. The rest run on
#: their own scale - an RSI is 0 to 100, a pivot gap is a percentage of price -
#: and drawn against Bitcoin either would be a flat line along the bottom with
#: every candle collapsed above it.
ON_PRICE = frozenset({"ema", "sma"})


@dataclass(frozen=True)
class Drawn:
    """One indicator, aligned bar for bar with the candles it belongs to."""

    label: str
    name: str
    length: int
    interval: Interval | None
    on_price: bool
    values: Line


def label_for(name: str, length: int, interval: Interval | None, base: Interval) -> str:
    """What to call it on a chart, in the words the builder uses."""
    where = f" {interval}" if interval and interval != base else ""
    if name == "pivot_gap":
        return f"Pivot gap %{where}"
    if name == "pivot_gap_rank":
        return f"Pivot gap percentile {length}{where}"
    return f"{name.upper()} {length}{where}"


def read(frame: Frame, name: str, length: int) -> float | None:
    """One indicator's value at the frame's cursor."""
    if name == "ema":
        return frame.ema(length)
    if name == "sma":
        return frame.sma(length)
    if name == "rsi":
        return frame.rsi(length)
    if name == "atr":
        return frame.atr(length)
    if name == "pivot_gap":
        return frame.pivot_gap()
    if name == "pivot_gap_rank":
        return frame.pivot_gap_rank(length)
    return None


def compute(
    bars: Sequence[Bar],
    base: Interval,
    wanted: Sequence[Wanted],
    at: Sequence[int] | None = None,
) -> list[Drawn]:
    """Each indicator's value at each of `at`, or at every bar.

    `at` exists so a chart can be computed over a long series and returned for a
    short window: an exponential average started at the window's edge is not the
    same number as one that has been running, and the difference is what would
    show up as a line crossing a bar too soon.
    """
    if not wanted or not bars:
        return []

    context = tuple(dict.fromkeys(i for _, _, i in wanted if i is not None and i != base))
    view, cursors = build(bars, base, context)
    indexes = list(at) if at is not None else list(range(len(bars)))

    collected: dict[Wanted, Line] = {key: [] for key in wanted}
    for i in indexes:
        wind(view, cursors, i)
        for key in wanted:
            name, length, interval = key
            frame = view.frame(interval) if interval and interval != base else view.base
            collected[key].append(read(frame, name, length))

    return [
        Drawn(
            label=label_for(name, length, interval, base),
            name=name,
            length=length,
            interval=interval,
            on_price=name in ON_PRICE,
            values=values,
        )
        for (name, length, interval), values in collected.items()
    ]


def parse_wanted(raw: str, base: Interval) -> list[Wanted]:
    """Indicators named in a query string: "ema:20,ema:50,rsi:14,ema:50:1h".

    A compact spelling for a chart, where the alternative is posting a document
    to draw a line. Anything unreadable is dropped rather than refused: a chart
    is worth drawing without an indicator somebody mistyped, and the endpoint
    that must be strict about this - the one that runs a strategy - already is.
    """
    from backtest.spec import DRAWABLE

    out: list[Wanted] = []
    for piece in raw.split(","):
        parts = piece.strip().split(":")
        if not parts or not parts[0]:
            continue
        name = parts[0].lower()
        if name not in DRAWABLE:
            continue
        try:
            length = int(parts[1]) if len(parts) > 1 and parts[1] else 14
        except ValueError:
            continue
        if length < 0 or length > 5000:
            continue
        interval: Interval | None = None
        if len(parts) > 2 and parts[2]:
            try:
                interval = Interval(parts[2])
            except ValueError:
                continue
            if interval.seconds < base.seconds:
                continue
        if (name, length, interval) not in out:
            out.append((name, length, interval))
    # Slowest first, so a fast line crossing a slow one stays legible.
    return sorted(out, key=lambda i: (-i[1], i[0]))
