"""Indicators, and the one property the engine's speed depends on.

The hand-computed cases are small enough to check on paper, which is the point:
an indicator verified only against another implementation is verified against
whatever that one gets wrong.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest

from analytics.indicators import (
    Line,
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
    supertrend,
    true_range,
    vwap,
    zscore,
)
from marketdata.models import Bar

START = datetime(2026, 1, 1, tzinfo=UTC)


def _bars(rows: list[tuple[float, float, float, float]]) -> list[Bar]:
    """Bars from (open, high, low, close), a minute apart."""
    return [
        Bar(ts=START + timedelta(minutes=i), open=o, high=h, low=lo, close=c, volume=1.0)
        for i, (o, h, lo, c) in enumerate(rows)
    ]


def _walk(n: int, seed: int = 7) -> list[Bar]:
    """A price series that wanders, for the property tests."""
    rng = random.Random(seed)
    price = 100.0
    out: list[Bar] = []
    for i in range(n):
        price = max(1.0, price * (1 + rng.gauss(0, 0.004)))
        high = price * (1 + abs(rng.gauss(0, 0.002)))
        low = price * (1 - abs(rng.gauss(0, 0.002)))
        out.append(
            Bar(
                ts=START + timedelta(minutes=i),
                open=price,
                high=high,
                low=low,
                close=price,
                volume=1.0,
            )
        )
    return out


# --------------------------------------------------------------------------
# The invariant everything else rests on
# --------------------------------------------------------------------------


@pytest.mark.parametrize("cut", [30, 61, 100, 137, 199, 200, 201, 260])
def test_no_indicator_can_see_past_the_bar_it_is_computed_for(cut: int) -> None:
    """Truncating the future must not change the past.

    This is the test that makes the engine's shape legitimate. Indicators are
    computed once over the whole series and then read by index, which is only the
    same thing as computing them from a growing window if no value depends on a
    later bar. If that ever stops being true, a backtest starts trading on
    information it could not have had, and the result looks wonderful.
    """
    bars = _walk(300)
    prices = closes(bars)

    everything: dict[str, Line] = {
        "sma": sma(prices, 20),
        "ema": ema(prices, 20),
        "rsi": rsi(prices, 14),
        "atr": atr(bars, 14),
        "tr": true_range(bars),
        "supertrend": supertrend(bars, 10, 3.0),
    }
    truncated: dict[str, Line] = {
        "sma": sma(prices[:cut], 20),
        "ema": ema(prices[:cut], 20),
        "rsi": rsi(prices[:cut], 14),
        "atr": atr(bars[:cut], 14),
        "tr": true_range(bars[:cut]),
        "supertrend": supertrend(bars[:cut], 10, 3.0),
    }

    for name, line in everything.items():
        assert line[:cut] == truncated[name], f"{name} changed when the future was removed"


@pytest.mark.parametrize("length", [1, 5, 14, 200])
def test_every_line_is_as_long_as_its_input(length: int) -> None:
    """Index i of a result belongs to bar i, so no caller has to track an offset."""
    bars = _walk(250)
    prices = closes(bars)

    assert len(sma(prices, length)) == len(bars)
    assert len(ema(prices, length)) == len(bars)
    assert len(rsi(prices, length)) == len(bars)
    assert len(atr(bars, length)) == len(bars)


@pytest.mark.parametrize("length", [5, 14, 200])
def test_warm_up_is_none_rather_than_a_partial_answer(length: int) -> None:
    """A 200-period average does not exist at bar 3, and must not pretend to."""
    prices = closes(_walk(250))

    line = sma(prices, length)

    assert all(v is None for v in line[: length - 1])
    assert line[length - 1] is not None


def test_a_series_shorter_than_the_period_is_all_none() -> None:
    prices = [1.0, 2.0, 3.0]

    assert sma(prices, 10) == [None, None, None]
    assert ema(prices, 10) == [None, None, None]
    assert rsi(prices, 10) == [None, None, None]


@pytest.mark.parametrize("length", [0, -1])
def test_a_period_of_zero_is_refused(length: int) -> None:
    with pytest.raises(ValueError, match="not a period"):
        sma([1.0, 2.0], length)


# --------------------------------------------------------------------------
# Values, by hand
# --------------------------------------------------------------------------


def test_simple_moving_average() -> None:
    assert sma([1.0, 2.0, 3.0, 4.0, 5.0], 3) == [None, None, 2.0, 3.0, 4.0]


def test_exponential_moving_average_is_seeded_with_a_simple_average() -> None:
    """[1,2,3,4,5] at length 3: seed (1+2+3)/3 = 2, then k = 0.5."""
    assert ema([1.0, 2.0, 3.0, 4.0, 5.0], 3) == [None, None, 2.0, 3.0, 4.0]


def test_the_ema_seed_is_not_the_first_value() -> None:
    """Seeding with values[0] would put 10 here, and drag the line for many bars."""
    line = ema([10.0, 20.0, 30.0], 3)

    assert line[2] == 20.0


def test_rsi_by_hand() -> None:
    """Changes +1, -0.5, +1 over three periods, then -0.5 and +1.

    Wilder's smoothing carries the averages forward: RS of 4, then 1.6, then 3.4.
    """
    line = rsi([10.0, 11.0, 10.5, 11.5, 11.0, 12.0], 3)

    assert line[:3] == [None, None, None]
    assert line[3] == pytest.approx(80.0)
    assert line[4] == pytest.approx(61.53846153846154)
    assert line[5] == pytest.approx(77.27272727272727)


def test_rsi_uses_wilders_smoothing_not_an_ema() -> None:
    """The two differ, and every chart package means Wilder's.

    Wilder divides by `length`; an EMA would use 2/(length+1). With the same
    period the EMA reacts faster, so a fall after a rise lands lower.
    """
    prices = [10.0, 11.0, 12.0, 13.0, 14.0, 10.0]

    wilders = rsi(prices, 4)[5]
    assert wilders is not None

    average_gain, average_loss = 1.0, 0.0
    ema_gain = (0.0 - average_gain) * (2 / 5) + average_gain
    ema_loss = (4.0 - average_loss) * (2 / 5) + average_loss
    as_an_ema = 100.0 - 100.0 / (1 + ema_gain / ema_loss)

    assert wilders > as_an_ema


def test_rsi_at_its_limits() -> None:
    rising = [float(i) for i in range(1, 20)]
    falling = list(reversed(rising))
    flat = [5.0] * 20

    assert rsi(rising, 14)[-1] == 100.0
    assert rsi(falling, 14)[-1] == 0.0
    # No direction at all is neutral, not maximally overbought.
    assert rsi(flat, 14)[-1] == 50.0


def test_true_range_counts_a_gap() -> None:
    """A bar that opens above the last close travelled further than its own range."""
    bars = _bars([(10.0, 11.0, 9.0, 10.0), (20.0, 21.0, 19.5, 20.0)])

    line = true_range(bars)

    assert line[0] is None  # no previous close to gap from
    # its own range is 1.5, but it is 11 away from the close before it
    assert line[1] == pytest.approx(11.0)


def test_average_true_range_starts_after_a_full_window() -> None:
    """The first bar has no true range, so the first average ends one bar later."""
    bars = _walk(40)

    line = atr(bars, 14)

    assert line[13] is None
    assert line[14] is not None


def test_pivots_by_hand() -> None:
    """High 110, low 90, close 100: pivot 100, span 20."""
    yesterday = _bars([(95.0, 110.0, 90.0, 100.0)])[0]

    levels = pivots(yesterday)

    assert levels.pivot == pytest.approx(100.0)
    assert levels.r1 == pytest.approx(110.0)
    assert levels.s1 == pytest.approx(90.0)
    assert levels.r2 == pytest.approx(120.0)
    assert levels.s2 == pytest.approx(80.0)
    assert levels.r3 == pytest.approx(130.0)
    assert levels.s3 == pytest.approx(70.0)


def test_pivot_levels_are_ordered() -> None:
    """S3 below S2 below S1 below the pivot, and up the other side."""
    levels = pivots(_bars([(95.0, 112.0, 88.0, 103.0)])[0])

    ordered = [levels.s3, levels.s2, levels.s1, levels.pivot, levels.r1, levels.r2, levels.r3]

    assert ordered == sorted(ordered)
    assert list(levels.levels) == ["S3", "S2", "S1", "P", "R1", "R2", "R3"]


def test_rsi_against_wilders_published_example() -> None:
    """The series every RSI reference reproduces, and a note on the last decimal.

    Widely published tables give 70.53 for the first value where this gives 70.46.
    The difference is in their data, not the formula: the fourteen changes implied
    by the closes printed alongside those tables total 1.40 of loss, and the tables
    state an average loss of 0.0993, which is 1.39/14. Recomputed from the closes
    as printed, 3.34 of gain against 1.40 of loss is an RS of 2.3857 and an RSI of
    70.4641 - which is what this returns.

    Pinned here because the temptation on seeing 70.46 next to a published 70.53 is
    to go looking for a bug in the implementation, and there isn't one.
    """
    closing = [
        44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08,
        45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64,
    ]

    line = rsi(closing, 14)

    assert line[14] == pytest.approx(70.4641, abs=0.0001)
    # and it keeps tracking the published shape from there
    assert line[15] == pytest.approx(66.25, abs=0.01)
    assert line[16] == pytest.approx(66.48, abs=0.01)


# --------------------------------------------------------------------------
# The pivot gap, and what it actually measures
# --------------------------------------------------------------------------


def test_the_s1_to_r1_width_is_the_previous_range() -> None:
    """The identity worth knowing before trusting the name.

    R1 = 2P - L and S1 = 2P - H, so R1 - S1 = H - L exactly. The pivot cancels,
    and this is a volatility measure wearing a pivot's name.
    """
    yesterday = _bars([(95.0, 112.0, 88.0, 103.0)])[0]

    levels = pivots(yesterday)

    assert levels.r1 - levels.s1 == pytest.approx(yesterday.high - yesterday.low)


def test_pivot_gap_is_that_width_over_the_pivot() -> None:
    """High 110, low 90, close 100: pivot 100, range 20, so 20%."""
    bars = _bars([(95.0, 110.0, 90.0, 100.0), (100.0, 101.0, 99.0, 100.0)])

    line = pivot_gap(bars)

    assert line[0] is None  # nothing before the first bar to take a pivot from
    assert line[1] == pytest.approx(20.0)


def test_pivot_gap_is_comparable_across_price_levels() -> None:
    """The reason for dividing by the pivot at all: the same proportional range
    at two very different prices has to read the same."""
    cheap = _bars([(95.0, 110.0, 90.0, 100.0), (100.0, 100.0, 100.0, 100.0)])
    dear = _bars([(950.0, 1100.0, 900.0, 1000.0), (1000.0, 1000.0, 1000.0, 1000.0)])

    assert pivot_gap(cheap)[1] == pytest.approx(pivot_gap(dear)[1])


def test_percentile_rank_puts_the_lowest_at_zero_and_the_highest_at_one_hundred() -> None:
    values: list[float | None] = [5.0, 4.0, 3.0, 2.0, 1.0, 0.5, 9.0]

    line = percentile_rank(values, 5)

    assert line[:5] == [None] * 5
    assert line[5] == 0.0  # lower than all five before it
    assert line[6] == 100.0  # higher than all five before it


def test_percentile_rank_counts_only_what_is_strictly_below() -> None:
    """A day that ties the quietest in the window reads as 0, not just above it."""
    values: list[float | None] = [1.0, 2.0, 3.0, 4.0, 1.0]

    assert percentile_rank(values, 4)[4] == 0.0


def test_percentile_rank_is_measured_against_what_came_before() -> None:
    """Not against a window including today, which could never reach its own
    extremes."""
    values: list[float | None] = [10.0, 20.0, 30.0, 40.0]

    line = percentile_rank(values, 2)

    # at index 2, the two before it are 10 and 20, and 30 beats both
    assert line[2] == 100.0


def test_percentile_rank_needs_a_full_window() -> None:
    """A rank against three days when sixty were asked for is not a rank."""
    values: list[float | None] = [1.0, 2.0, 3.0]

    assert percentile_rank(values, 60) == [None, None, None]


def test_percentile_rank_skips_bars_with_nothing_to_rank() -> None:
    values: list[float | None] = [1.0, 2.0, 3.0, None]

    assert percentile_rank(values, 3)[3] is None


def test_a_squeeze_reads_near_zero() -> None:
    """The setup this exists for: a quiet day after a run of wide ones."""
    wide = [(100.0, 110.0, 90.0, 100.0)] * 20
    quiet = [(100.0, 100.5, 99.5, 100.0)]
    bars = _bars(wide + quiet + [(100.0, 100.0, 100.0, 100.0)])

    rank = pivot_gap_rank(bars, 10)

    assert rank[-1] == 0.0


def test_the_gap_and_its_rank_cannot_see_the_bar_they_are_on() -> None:
    """The same rule every other indicator here follows."""
    bars = _walk(300)

    everything = pivot_gap_rank(bars, 20)
    truncated = pivot_gap_rank(bars[:150], 20)

    assert everything[:150] == truncated


def test_supertrend_starts_above_price_and_flips_below_on_a_close_through_it() -> None:
    """Flat at 100 with a 2-point range: ATR(2) is 2, the bands 100 +/- 6, and
    the line starts on the upper one, 106. A close at 110 crosses it, so the
    line jumps to the lower band. That bar's own lower band would be 89 (its
    midpoint 108.5 less three of its ATR), but a lower band only ever rises
    while price stays above it, so the line is the 94 it already stood at."""
    rows = [(100.0, 101.0, 99.0, 100.0)] * 4 + [(100.0, 111.0, 106.0, 110.0)]
    line = supertrend(_bars(rows), length=2, multiplier=3.0)
    assert line[:2] == [None, None]
    assert line[2] == pytest.approx(106.0)
    assert line[3] == pytest.approx(106.0)
    # True range of the jump bar: max(5, 11, 6) = 11; ATR = (2 + 11) / 2 = 6.5.
    assert 108.5 - 3 * 6.5 == pytest.approx(89.0)
    assert line[4] == pytest.approx(94.0)
    assert line[4] < rows[4][3]


# --------------------------------------------------------------------------
# MACD, Bollinger bands, ADX, VWAP and channels
# --------------------------------------------------------------------------


@pytest.mark.parametrize("cut", [40, 77, 150, 199, 260])
def test_the_newer_indicators_cannot_see_past_their_bar_either(cut: int) -> None:
    bars = _walk(300)
    prices = closes(bars)

    def lines(upto: int) -> dict[str, Line]:
        b, p = bars[:upto], prices[:upto]
        line, signal, hist = macd(p, 12, 26, 9)
        mid, upper, lower = bollinger(p, 20, 2.0)
        trend, plus, minus = adx(b, 14)
        return {
            "macd": line,
            "macd_signal": signal,
            "macd_hist": hist,
            "bb_mid": mid,
            "bb_upper": upper,
            "bb_lower": lower,
            "adx": trend,
            "plus_di": plus,
            "minus_di": minus,
            "vwap": vwap(b),
            "highest": highest([x.high for x in b], 20),
            "lowest": lowest([x.low for x in b], 20),
        }

    whole, cutoff = lines(300), lines(cut)
    for name, line in whole.items():
        assert line[:cut] == cutoff[name], f"{name} changed when the future was removed"
        assert len(line) == 300


def test_macd_is_fast_ema_minus_slow_ema_and_the_signal_smooths_it() -> None:
    prices = closes(_walk(120))
    line, signal, hist = macd(prices, 12, 26, 9)
    quick, steady = ema(prices, 12), ema(prices, 26)

    assert line[24] is None
    assert line[25] == pytest.approx((quick[25] or 0) - (steady[25] or 0))
    # The signal needs nine line values before it exists: 25 + 8.
    assert signal[32] is None
    assert signal[33] is not None
    seed = sum(v for v in line[25:34] if v is not None) / 9
    assert signal[33] == pytest.approx(seed)
    assert hist[40] == pytest.approx((line[40] or 0) - (signal[40] or 0))


def test_bollinger_bands_by_hand() -> None:
    mid, upper, lower = bollinger([1.0, 2.0, 3.0, 4.0], 3, 2.0)

    assert mid[:2] == [None, None]
    assert mid[2] == pytest.approx(2.0)
    # Population deviation of 1, 2, 3 is sqrt(2/3).
    assert upper[2] == pytest.approx(2.0 + 2 * (2 / 3) ** 0.5)
    assert lower[2] == pytest.approx(2.0 - 2 * (2 / 3) ** 0.5)
    assert mid[3] == pytest.approx(3.0)


def test_bollinger_bands_survive_large_prices_and_a_flat_series() -> None:
    flat = bollinger([100_000.0] * 30, 20, 2.0)
    assert flat[1][25] == pytest.approx(100_000.0)
    assert flat[2][25] == pytest.approx(100_000.0)

    big = [100_000.0 + (i % 5) for i in range(60)]
    _, upper, lower = bollinger(big, 20, 2.0)
    width = (upper[40] or 0) - (lower[40] or 0)
    assert 0 < width < 20


def test_highest_and_lowest_include_the_current_bar() -> None:
    values = [1.0, 3.0, 2.0, 5.0, 4.0]
    assert highest(values, 3) == [None, None, 3.0, 5.0, 5.0]
    assert lowest(values, 3) == [None, None, 1.0, 2.0, 2.0]
    assert highest(values, 1) == values


def test_highest_matches_a_brute_force_window() -> None:
    values = [b.high for b in _walk(200)]
    fast = highest(values, 17)
    for i in range(16, 200):
        assert fast[i] == max(values[i - 16 : i + 1])


def test_adx_starts_after_two_periods_and_stays_in_range() -> None:
    trend, plus, minus = adx(_walk(200), 14)

    assert plus[13] is None and plus[14] is not None
    assert trend[26] is None and trend[27] is not None
    for line in (trend, plus, minus):
        assert all(0.0 <= v <= 100.0 for v in line if v is not None)


def test_adx_is_high_in_a_steady_trend_and_says_which_way() -> None:
    rising = _bars([(i, i + 1.0, i - 0.5, i + 0.5) for i in range(1, 80)])
    trend, plus, minus = adx(rising, 14)

    assert (trend[-1] or 0.0) > 80
    assert (plus[-1] or 0) > (minus[-1] or 0)

    falling = _bars([(200 - i, 201 - i, 199.5 - i, 200.5 - i - 1) for i in range(80)])
    _, plus_down, minus_down = adx(falling, 14)
    assert (minus_down[-1] or 0) > (plus_down[-1] or 0)


def test_vwap_weights_by_volume_and_restarts_each_day() -> None:
    day = datetime(2026, 1, 1, 23, 0, tzinfo=UTC)
    bars = [
        Bar(ts=day, open=10, high=10, low=10, close=10, volume=1.0),
        Bar(ts=day + timedelta(minutes=30), open=20, high=20, low=20, close=20, volume=3.0),
        # Past midnight UTC: a new day, so the earlier bars no longer count.
        Bar(ts=day + timedelta(hours=1), open=50, high=50, low=50, close=50, volume=2.0),
    ]

    line = vwap(bars)

    assert line[0] == pytest.approx(10.0)
    assert line[1] == pytest.approx((10 * 1 + 20 * 3) / 4)
    assert line[2] == pytest.approx(50.0)


def test_vwap_is_undefined_until_some_volume_has_traded() -> None:
    bars = [
        Bar(ts=START, open=1, high=1, low=1, close=1, volume=0.0),
        Bar(ts=START + timedelta(minutes=1), open=2, high=2, low=2, close=2, volume=4.0),
    ]
    assert vwap(bars) == [None, pytest.approx(2.0)]


def _brute_rank(values: list[float | None], length: int) -> Line:
    """The definition, written the slow way: count what was strictly lower."""
    out: Line = [None] * len(values)
    for i in range(length, len(values)):
        current = values[i]
        window = values[i - length : i]
        if current is None or any(v is None for v in window):
            continue
        out[i] = sum(1 for v in window if v is not None and v < current) / length * 100.0
    return out


@pytest.mark.parametrize("length", [1, 3, 20, 60])
def test_the_fast_percentile_rank_is_the_definition(length: int) -> None:
    rng = random.Random(length)
    values: list[float | None] = [round(rng.gauss(0, 1), 1) for _ in range(300)]  # ties
    for gap in (0, 1, 2, 77, 150, 151, 152, 299):
        values[gap] = None

    assert percentile_rank(values, length) == _brute_rank(values, length)


def test_realised_volatility_by_hand() -> None:
    # Returns of +10%, -10%, +10%: mean 0.0333, sample deviation of those three.
    prices = [100.0, 110.0, 99.0, 108.9]
    line = realised_volatility(prices, 3)
    returns = [0.1, -0.1, 0.1]
    mean = sum(returns) / 3
    expected = (sum((r - mean) ** 2 for r in returns) / 2) ** 0.5

    assert line[:3] == [None, None, None]
    assert line[3] == pytest.approx(expected)


def test_zscore_by_hand_and_flat_series() -> None:
    line = zscore([1.0, 2.0, 3.0, 6.0], 3)

    assert line[:2] == [None, None]
    assert line[2] == pytest.approx(1.0)  # 3 is one deviation (1.0) above the mean 2
    # Window 2, 3, 6: mean 11/3, sample variance 13/3, and 6 sits 7/3 above the mean.
    assert line[3] == pytest.approx((7 / 3) / (13 / 3) ** 0.5)
    assert zscore([5.0] * 10, 4)[-1] == 0.0


@pytest.mark.parametrize("cut", [50, 123, 250])
def test_the_feature_lines_cannot_see_past_their_bar(cut: int) -> None:
    prices = closes(_walk(300))
    volumes = [b.volume + i % 7 for i, b in enumerate(_walk(300))]
    assert realised_volatility(prices, 20)[:cut] == realised_volatility(prices[:cut], 20)
    assert zscore(volumes, 20)[:cut] == zscore(volumes[:cut], 20)
    ranked = percentile_rank(realised_volatility(prices, 20), 30)
    assert ranked[:cut] == percentile_rank(realised_volatility(prices[:cut], 20), 30)
