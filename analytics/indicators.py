"""Moving averages, RSI, ATR and pivots.

Three rules hold for everything in this module, and the engine depends on all
three.

**Causal.** The value at index `i` is computed from bars up to and including `i`
and from nothing later. This is the difference between a backtest and a fantasy,
so it is not left to care: `tests/analytics/test_indicators.py` asserts it for
every function by computing each series twice, once over the whole history and
once over a truncated copy, and requiring the overlapping values to be identical.

**Aligned.** Every function returns a list the same length as its input, so index
`i` of the result belongs to bar `i` and no caller has to reason about an offset.

**Honest about warm-up.** A 200-period average does not exist until 200 periods
have passed, and those entries are `None` rather than zero, the first price, or a
partial average computed from fewer bars. A rule that receives a number cannot
tell it was a guess; a rule that receives `None` has to decide what to do, which
is the point. Most of them will wait.

That the values are computed over the whole series in one pass and then indexed -
rather than recomputed from a growing window at each step - is what keeps a
backtest linear instead of quadratic. It is only legitimate because of the first
rule: for a causal indicator, reading index `i` of the full array gives exactly
the number that computing over `bars[:i+1]` would have given.

No numpy, in keeping with the rest of `analytics/`. These are a handful of
recurrences over a few hundred thousand floats.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from marketdata.models import Bar

#: A series with gaps: `None` wherever the indicator is not yet defined.
Line = list[float | None]


def sma(values: Sequence[float], length: int) -> Line:
    """Simple moving average over the last `length` values."""
    _check(length)
    out: Line = [None] * len(values)
    if len(values) < length:
        return out
    running = sum(values[:length])
    out[length - 1] = running / length
    for i in range(length, len(values)):
        # A rolling sum rather than a fresh one each step: over 300,000 bars a
        # 200-period average is 60 million additions this way round and a few
        # hundred thousand that way. It is not bit-for-bit what re-summing each
        # window gives - the running total carries rounding forward - but it is
        # stable under truncation, which is the property that matters here: the
        # arithmetic reaching index i is the same whether or not the series
        # continues past it, so the causality test holds exactly.
        running += values[i] - values[i - length]
        out[i] = running / length
    return out


def ema(values: Sequence[float], length: int) -> Line:
    """Exponential moving average, seeded with a simple average.

    The seed is a real choice and platforms differ on it. Seeding with the first
    value alone lets that one price dominate for dozens of bars, which shows up as
    a spurious signal at the start of every backtest; seeding with the simple
    average of the first `length` values is what TradingView and pandas'
    `adjust=False` do, and it is what a reader comparing a chart to a result will
    expect. The two converge, but not for several multiples of `length`, which is
    exactly the region a short backtest lives in.
    """
    _check(length)
    out: Line = [None] * len(values)
    if len(values) < length:
        return out
    k = 2.0 / (length + 1)
    previous = sum(values[:length]) / length
    out[length - 1] = previous
    for i in range(length, len(values)):
        previous = (values[i] - previous) * k + previous
        out[i] = previous
    return out


def rsi(values: Sequence[float], length: int = 14) -> Line:
    """Wilder's relative strength index, on a 0-100 scale.

    Wilder's smoothing, not an exponential average of the same length: his
    recurrence uses 1/length where an EMA would use 2/(length+1), so an "EMA RSI"
    of 14 is really Wilder's of about 27 and reads visibly differently. Every
    chart package means this one by "RSI", so this is the one to implement.

    100 when nothing has fallen across the window and 0 when nothing has risen -
    the limits the formula tends to, taken directly to avoid dividing by zero. A
    series that has not moved at all is 50 rather than either: it is the one case
    with no direction in it, and calling that maximally overbought - which the
    zero-denominator reading would - fires every rule that watches for an extreme.
    """
    _check(length)
    out: Line = [None] * len(values)
    if len(values) <= length:
        return out

    gains = 0.0
    losses = 0.0
    for i in range(1, length + 1):
        change = values[i] - values[i - 1]
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    average_gain = gains / length
    average_loss = losses / length
    out[length] = _rsi_from(average_gain, average_loss)

    for i in range(length + 1, len(values)):
        change = values[i] - values[i - 1]
        average_gain = (average_gain * (length - 1) + max(change, 0.0)) / length
        average_loss = (average_loss * (length - 1) + max(-change, 0.0)) / length
        out[i] = _rsi_from(average_gain, average_loss)
    return out


def _rsi_from(average_gain: float, average_loss: float) -> float:
    if average_loss == 0.0:
        return 100.0 if average_gain > 0.0 else 50.0
    if average_gain == 0.0:
        return 0.0
    return 100.0 - 100.0 / (1.0 + average_gain / average_loss)


def true_range(bars: Sequence[Bar]) -> Line:
    """How far price actually travelled in each bar, gaps included.

    The bar's own range understates a bar that opened away from the last close,
    and a stop placed on the understatement is a stop that was never as wide as it
    looked. Undefined for the first bar, which has no previous close.
    """
    out: Line = [None] * len(bars)
    for i in range(1, len(bars)):
        previous_close = bars[i - 1].close
        out[i] = max(
            bars[i].high - bars[i].low,
            abs(bars[i].high - previous_close),
            abs(bars[i].low - previous_close),
        )
    return out


def atr(bars: Sequence[Bar], length: int = 14) -> Line:
    """Average true range, smoothed Wilder's way as he defined it."""
    _check(length)
    out: Line = [None] * len(bars)
    ranges = true_range(bars)
    # The first entry is None by definition, so the first full window ends here.
    first = length
    if len(bars) <= first:
        return out
    window = [r for r in ranges[1 : first + 1] if r is not None]
    if len(window) < length:
        return out
    previous = sum(window) / length
    out[first] = previous
    for i in range(first + 1, len(bars)):
        current = ranges[i]
        if current is None:
            continue
        previous = (previous * (length - 1) + current) / length
        out[i] = previous
    return out


def supertrend(bars: Sequence[Bar], length: int = 10, multiplier: float = 3.0) -> Line:
    """The Supertrend line: below price in an uptrend, above it in a downtrend.

    TradingView's `ta.supertrend`: bands `multiplier` ATRs either side of the
    bar's midpoint, each allowed to move only towards price until a close
    crosses it, at which point the line jumps to the other band. It starts on
    the upper band - a downtrend - as TradingView's does, so price above the
    line reads as an uptrend and below it as a downtrend.
    """
    _check(length)
    out: Line = [None] * len(bars)
    ranges = atr(bars, length)
    upper = lower = line = 0.0
    started = False
    for i, bar in enumerate(bars):
        a = ranges[i]
        if a is None:
            continue
        mid = (bar.high + bar.low) / 2.0
        basic_upper, basic_lower = mid + multiplier * a, mid - multiplier * a
        if not started:
            upper, lower, line = basic_upper, basic_lower, basic_upper
            started = True
        else:
            previous_close = bars[i - 1].close
            was_upper = line == upper
            upper = basic_upper if basic_upper < upper or previous_close > upper else upper
            lower = basic_lower if basic_lower > lower or previous_close < lower else lower
            if was_upper:
                line = lower if bar.close > upper else upper
            else:
                line = upper if bar.close < lower else lower
        out[i] = line
    return out


@dataclass(frozen=True)
class Pivots:
    """Floor-trader pivots for one period, from the period before it.

    Causal by construction, which is why this takes a bar rather than a series:
    the levels that apply today come from yesterday's finished bar, and there is
    no version of this that could read the present period by accident.
    """

    pivot: float
    r1: float
    r2: float
    r3: float
    s1: float
    s2: float
    s3: float

    @property
    def levels(self) -> dict[str, float]:
        """Every level by name, for a rule that wants the nearest one."""
        return {
            "S3": self.s3,
            "S2": self.s2,
            "S1": self.s1,
            "P": self.pivot,
            "R1": self.r1,
            "R2": self.r2,
            "R3": self.r3,
        }


def pivots(previous: Bar) -> Pivots:
    """The classic levels, from a completed bar of the higher timeframe.

    The original floor-trader formulas, not Fibonacci, Camarilla or Woodie's
    variants - those are different indicators that share a name, and picking one
    silently would make two results incomparable.
    """
    pivot = (previous.high + previous.low + previous.close) / 3.0
    span = previous.high - previous.low
    return Pivots(
        pivot=pivot,
        r1=2.0 * pivot - previous.low,
        s1=2.0 * pivot - previous.high,
        r2=pivot + span,
        s2=pivot - span,
        r3=previous.high + 2.0 * (pivot - previous.low),
        s3=previous.low - 2.0 * (previous.high - pivot),
    )


def pivot_gap(bars: Sequence[Bar]) -> Line:
    """The distance from S1 to R1, as a percentage of the pivot.

    Worth being straight about what this measures, because the algebra is not
    obvious and the name suggests something it is not. With standard pivots
    R1 = 2P - L and S1 = 2P - H, so:

        R1 - S1 = (2P - L) - (2P - H) = H - L

    The pivot cancels exactly. The S1-R1 width *is* the previous period's range,
    and nothing about the pivot formula survives into it. Dividing by the pivot is
    what makes the figure worth having: a 2,000 point range is wide on Bitcoin at
    30,000 and narrow at 120,000, and only the percentage is comparable across a
    three-year window.

    So this is a volatility measure wearing a pivot's name. That is fine - a
    narrow prior range before an expansion is one of the older setups there is -
    but a rule built on it should know it is trading volatility rather than
    anything about support and resistance.

    Causal by construction: the value at bar `i` comes from bar `i - 1`, which is
    the same rule the pivot levels themselves follow.
    """
    out: Line = [None] * len(bars)
    for i in range(1, len(bars)):
        previous = bars[i - 1]
        pivot = (previous.high + previous.low + previous.close) / 3.0
        if pivot <= 0:
            continue
        out[i] = (previous.high - previous.low) / pivot * 100.0
    return out


def percentile_rank(values: Sequence[float | None], length: int) -> Line:
    """Where each value stands among the `length` before it, from 0 to 100.

    0 means nothing in the window was lower - today is the narrowest, the
    quietest, the smallest of the last `length`. 100 means nothing was higher.

    Compared against the *previous* `length` values and not against a window that
    includes today, so the answer is "how does today compare with what came
    before" rather than a figure that can never quite reach its own extremes.
    Strictly less than, so a day that ties the quietest in the window reads as 0
    rather than as slightly above it.

    Undefined until there are `length` earlier values to compare against, and at
    any bar whose own value is undefined - a rank against nothing is not zero.
    """
    _check(length)
    out: Line = [None] * len(values)
    for i in range(length, len(values)):
        current = values[i]
        if current is None:
            continue
        window = [v for v in values[i - length : i] if v is not None]
        if len(window) < length:
            continue
        below = sum(1 for v in window if v < current)
        out[i] = below / length * 100.0
    return out


def pivot_gap_rank(bars: Sequence[Bar], length: int = 60) -> Line:
    """How today's S1-R1 width compares with the last `length` periods.

    The squeeze reading: a rank near 0 says the previous period's range was among
    the narrowest of the window, which is the condition a range expansion tends to
    follow. Near 100 says the opposite, and a rule that enters on a breakout there
    is buying after the move rather than before it.

    Read on the timeframe the pivots are drawn on - daily, for a daily pivot - so
    `length` counts days rather than bars of whatever is being traded.
    """
    return percentile_rank(pivot_gap(bars), length)


def closes(bars: Sequence[Bar]) -> list[float]:
    """Closing prices, the input most of these want."""
    return [b.close for b in bars]


def _check(length: int) -> None:
    if length < 1:
        raise ValueError(f"a period of {length} is not a period")
