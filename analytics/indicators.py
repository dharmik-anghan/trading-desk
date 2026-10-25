"""Moving averages, RSI, ATR, pivots, and the other indicators a rule can read.

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

from bisect import bisect_left, insort
from collections import deque
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

    The window is kept sorted, so each step is a binary search and one insertion
    rather than a pass over the window: a 500-bar rank over 300,000 bars is the
    difference between a second and a quarter of an hour.
    """
    _check(length)
    out: Line = [None] * len(values)
    window: list[float] = []
    gaps = 0
    for i in range(1, len(values)):
        # The window for bar i is bars i-length .. i-1: bar i-1 enters, and bar
        # i-length-1 has just left.
        entering = values[i - 1]
        if entering is None:
            gaps += 1
        else:
            insort(window, entering)
        if i - length - 1 >= 0:
            leaving = values[i - length - 1]
            if leaving is None:
                gaps -= 1
            else:
                del window[bisect_left(window, leaving)]
        current = values[i]
        if i < length or current is None or gaps or len(window) < length:
            continue
        out[i] = bisect_left(window, current) / length * 100.0
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



def macd(
    values: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[Line, Line, Line]:
    """MACD: the line, its signal line and the histogram between them.

    The line is the fast EMA minus the slow one, so it exists from the first bar
    both averages do. The signal line is an EMA of the line, seeded with a simple
    average of its first `signal` values exactly as `ema` seeds - it is `ema`
    applied to the defined part of the line, not a different recipe.
    """
    _check(fast)
    _check(slow)
    _check(signal)
    quick, steady = ema(values, fast), ema(values, slow)
    line: Line = [
        a - b if a is not None and b is not None else None
        for a, b in zip(quick, steady, strict=True)
    ]
    start = next((i for i, v in enumerate(line) if v is not None), len(line))
    smoothed = ema([v for v in line[start:] if v is not None], signal)
    sig: Line = [None] * start
    sig.extend(smoothed)
    hist: Line = [
        a - b if a is not None and b is not None else None
        for a, b in zip(line, sig, strict=True)
    ]
    return line, sig, hist


def bollinger(
    values: Sequence[float], length: int = 20, mult: float = 2.0
) -> tuple[Line, Line, Line]:
    """Bollinger bands: the middle (a simple average), then upper and lower.

    The standard deviation is the population one, as TradingView's `ta.bb` uses,
    over the same window as the average.

    Computed from running sums of the values taken relative to the first one.
    Prices near 100,000 squared are around 10^10, and a variance of a few thousand
    read off a difference of two such numbers loses digits it cannot spare;
    shifting by a constant does not change a variance and keeps the sums small.
    """
    _check(length)
    n = len(values)
    mid: Line = [None] * n
    upper: Line = [None] * n
    lower: Line = [None] * n
    if n < length:
        return mid, upper, lower
    origin = values[0]
    total = sum_sq = 0.0
    for i, raw in enumerate(values):
        x = raw - origin
        total += x
        sum_sq += x * x
        if i >= length:
            old = values[i - length] - origin
            total -= old
            sum_sq -= old * old
        if i >= length - 1:
            mean = total / length
            variance = max(0.0, sum_sq / length - mean * mean)
            width = mult * variance**0.5
            centre = mean + origin
            mid[i], upper[i], lower[i] = centre, centre + width, centre - width
    return mid, upper, lower


def adx(bars: Sequence[Bar], length: int = 14) -> tuple[Line, Line, Line]:
    """Wilder's directional movement: ADX, then +DI and -DI.

    ADX says how strongly price is trending and nothing about which way; the two
    DI lines say which way. Smoothed Wilder's way throughout, as `atr` is, so a
    14-period ADX here is the one every charting package draws.

    +DI and -DI exist from bar `length`, ADX from bar `2 * length - 1`: it is a
    smoothed average of DX and needs `length` of them first.
    """
    _check(length)
    n = len(bars)
    adx_line: Line = [None] * n
    plus: Line = [None] * n
    minus: Line = [None] * n
    if n <= length:
        return adx_line, plus, minus

    ranges = true_range(bars)
    tr_s = pdm_s = mdm_s = 0.0
    dx: list[float] = []
    average = 0.0
    for i in range(1, n):
        up = bars[i].high - bars[i - 1].high
        down = bars[i - 1].low - bars[i].low
        pdm = up if up > down and up > 0 else 0.0
        mdm = down if down > up and down > 0 else 0.0
        tr = ranges[i] or 0.0
        if i <= length:
            tr_s += tr
            pdm_s += pdm
            mdm_s += mdm
            if i < length:
                continue
        else:
            tr_s = tr_s - tr_s / length + tr
            pdm_s = pdm_s - pdm_s / length + pdm
            mdm_s = mdm_s - mdm_s / length + mdm
        pdi = 100.0 * pdm_s / tr_s if tr_s else 0.0
        mdi = 100.0 * mdm_s / tr_s if tr_s else 0.0
        plus[i], minus[i] = pdi, mdi
        total = pdi + mdi
        dx.append(100.0 * abs(pdi - mdi) / total if total else 0.0)
        if len(dx) == length:
            average = sum(dx) / length
            adx_line[i] = average
        elif len(dx) > length:
            average = (average * (length - 1) + dx[-1]) / length
            adx_line[i] = average
    return adx_line, plus, minus


def vwap(bars: Sequence[Bar]) -> Line:
    """Volume-weighted average price, restarted at each UTC midnight.

    The "session" is the UTC day, which is a session for a perpetual that trades
    around the clock and lands inside one for anything the desk trades in Asian
    or European hours. Typical price (high + low + close over three) weighted by
    volume. Undefined until the day has traded some volume - an average of
    nothing is not the close.
    """
    out: Line = [None] * len(bars)
    day = None
    value = volume = 0.0
    for i, bar in enumerate(bars):
        if bar.ts.date() != day:
            day = bar.ts.date()
            value = volume = 0.0
        value += (bar.high + bar.low + bar.close) / 3.0 * bar.volume
        volume += bar.volume
        if volume > 0:
            out[i] = value / volume
    return out


def highest(values: Sequence[float], length: int) -> Line:
    """The largest of the last `length` values, this one included."""
    return _extreme(values, length, largest=True)


def lowest(values: Sequence[float], length: int) -> Line:
    """The smallest of the last `length` values, this one included."""
    return _extreme(values, length, largest=False)


def _extreme(values: Sequence[float], length: int, *, largest: bool) -> Line:
    """A sliding maximum or minimum in one pass, with a deque of candidates.

    Each value is pushed once and popped at most once, so a 300-bar channel over
    300,000 bars is not 90 million comparisons.
    """
    _check(length)
    out: Line = [None] * len(values)
    window: deque[int] = deque()
    for i, v in enumerate(values):
        while window and (values[window[-1]] <= v if largest else values[window[-1]] >= v):
            window.pop()
        window.append(i)
        if window[0] <= i - length:
            window.popleft()
        if i >= length - 1:
            out[i] = values[window[0]]
    return out


def realised_volatility(values: Sequence[float], length: int = 20) -> Line:
    """Standard deviation of the last `length` one-bar returns, as a fraction.

    The returns are simple (`close / previous - 1`), so the first is defined at
    bar 1 and the first volatility at bar `length`.
    """
    _check(length)
    out: Line = [None] * len(values)
    returns = [
        values[i] / values[i - 1] - 1.0 if values[i - 1] else 0.0 for i in range(1, len(values))
    ]
    total = sum_sq = 0.0
    for k, r in enumerate(returns):
        total += r
        sum_sq += r * r
        if k >= length:
            old = returns[k - length]
            total -= old
            sum_sq -= old * old
        if k >= length - 1 and length > 1:
            mean = total / length
            variance = max(0.0, (sum_sq - length * mean * mean) / (length - 1))
            out[k + 1] = variance**0.5
    return out


def zscore(values: Sequence[float], length: int = 20) -> Line:
    """How many standard deviations the value is from the last `length`, itself included.

    Zero where the window is perfectly flat, because "no variation" is not a
    reason to call a value infinitely unusual.
    """
    _check(length)
    n = len(values)
    out: Line = [None] * n
    if n < length or length < 2:
        return out
    origin = values[0]
    total = sum_sq = 0.0
    for i, raw in enumerate(values):
        x = raw - origin
        total += x
        sum_sq += x * x
        if i >= length:
            old = values[i - length] - origin
            total -= old
            sum_sq -= old * old
        if i >= length - 1:
            mean = total / length
            variance = max(0.0, (sum_sq - length * mean * mean) / (length - 1))
            out[i] = (x - mean) / variance**0.5 if variance > 1e-18 else 0.0
    return out


def closes(bars: Sequence[Bar]) -> list[float]:
    """Closing prices, the input most of these want."""
    return [b.close for b in bars]


def volumes(bars: Sequence[Bar]) -> list[float]:
    return [b.volume for b in bars]


def _check(length: int) -> None:
    if length < 1:
        raise ValueError(f"a period of {length} is not a period")
