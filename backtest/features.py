"""What the entry filter is told about a signal.

A rule decides that this bar is a setup. The filter is asked whether this setup is
one of the good ones, and everything it is told about the moment comes from here.

**Closed bars only, by construction.** Every number is read through `View`, which
will not offer a bar that has not finished - the same guarantee a strategy's own
conditions have. There is no path from here to a bar after the decision, so there
is nothing to be careful about: a feature that looked ahead would have to bypass
the view, and `tests/backtest/test_features.py` checks the property directly by
changing every later bar and requiring the vector not to move.

**Unknown is NaN, not zero.** An indicator still warming up, or a higher timeframe
that has not closed its first bar, is missing, and the model is allowed to know it
is missing. A zero would claim to know something.

**Same vector on both sides of the signal.** The side the rule wants is part of
the vector: a long signal after a 5% drop and a short signal after the same drop
are different questions, and a model should not have to guess which it is looking
at.

Funding is not here. It belongs to the market, not to the bars, and `View` does not
reach the market - adding a path to it would break the guarantee above. A feature
for it would have to be handed in by the engine, which is a decision for when the
rest has shown it is worth it.
"""

from __future__ import annotations

import math

from backtest.market import Side
from backtest.view import Frame, View
from marketdata.models import Interval

NAN = math.nan

#: Timeframes read beyond the traded one, nearest first. Each adds a trend and a
#: momentum reading from a longer horizon than the rule trades.
LADDER = (Interval.H1, Interval.H4, Interval.D1)

#: How many higher timeframes feed the vector.
HIGHER = 2

#: Bars of volatility history the regime percentile is measured against.
REGIME_BARS = 500

BASE_NAMES = (
    "side",
    "ret_1",
    "ret_5",
    "ret_20",
    "ret_60",
    "atr_pct",
    "rvol",
    "rvol_rank",
    "rsi_14",
    "dist_ema_20",
    "dist_ema_50",
    "dist_ema_200",
    "vol_z",
    "pivot_pos",
    "pivot_gap_rank",
    "hour",
    "weekday",
)


def higher(base: Interval) -> tuple[Interval, ...]:
    """The longer timeframes the vector reads, for a rule trading `base`."""
    return tuple(i for i in LADDER if i.seconds > base.seconds)[:HIGHER]


def context(base: Interval) -> tuple[Interval, ...]:
    """What the engine must align for the vector to be readable.

    The daily frame is always included when above the base, because the pivot
    features are read from it.
    """
    frames = set(higher(base))
    if Interval.D1.seconds > base.seconds:
        frames.add(Interval.D1)
    return tuple(sorted(frames, key=lambda i: i.seconds))


def names(base: Interval) -> tuple[str, ...]:
    """Feature names, in the order `extract` returns them."""
    out = list(BASE_NAMES)
    for interval in higher(base):
        out += [f"rsi_14_{interval}", f"dist_ema_50_{interval}"]
    return tuple(out)


def extract(view: View, side: Side) -> list[float]:
    """The feature vector for a signal on the view's newest closed bar."""
    base = view.base
    close = base.close
    row: list[float] = [float(side.sign)]
    for back in (1, 5, 20, 60):
        row.append(_change(base, close, back))
    row.append(_ratio(base.atr(14), close, 100.0))
    row.append(_value(base.line("rvol", 20)))
    row.append(_value(base.line("rvol_rank", REGIME_BARS)))
    row.append(_value(base.rsi(14)))
    for length in (20, 50, 200):
        row.append(_distance(base, close, length))
    row.append(_value(base.line("vol_z", 20)))

    pivot_pos = NAN
    gap_rank = NAN
    if Interval.D1.seconds > base.interval.seconds and Interval.D1 in view.context:
        daily = view.frame(Interval.D1)
        levels = daily.pivots()
        if levels is not None and close is not None:
            support, resistance = levels.levels["S1"], levels.levels["R1"]
            if resistance > support:
                pivot_pos = (close - support) / (resistance - support)
        gap_rank = _value(daily.pivot_gap_rank(60))
    row += [pivot_pos, gap_rank]

    at = view.at
    row.append(float(at.hour) if at else NAN)
    row.append(float(at.weekday()) if at else NAN)

    for interval in higher(base.interval):
        frame = view.frame(interval)
        row.append(_value(frame.rsi(14)))
        row.append(_distance(frame, frame.close, 50))
    return row


def _value(x: float | None) -> float:
    return NAN if x is None else x


def _ratio(a: float | None, b: float | None, scale: float = 1.0) -> float:
    if a is None or not b:
        return NAN
    return a / b * scale


def _change(frame: Frame, close: float | None, back: int) -> float:
    """Return over `back` closed bars, as a fraction."""
    then = frame.price("close", back)
    if close is None or not then:
        return NAN
    return close / then - 1.0


def _distance(frame: Frame, close: float | None, length: int) -> float:
    """How far the close is from its EMA, as a fraction of the EMA."""
    average = frame.ema(length)
    if close is None or not average:
        return NAN
    return close / average - 1.0
