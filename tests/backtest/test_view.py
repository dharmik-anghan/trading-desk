"""The multi-timeframe layer, and the lookahead it exists to prevent."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from backtest.resample import bucket_for, closes_at, resample
from backtest.view import build, wind
from marketdata.models import Bar, Interval, Series

START = datetime(2026, 1, 1, tzinfo=UTC)


def _series(n: int, interval: Interval = Interval.M5, first: float = 100.0) -> list[Bar]:
    """A rising series, one bar per interval, each bar's range around its close."""
    out: list[Bar] = []
    for i in range(n):
        price = first + i
        out.append(
            Bar(
                ts=START + timedelta(seconds=interval.seconds * i),
                open=price,
                high=price + 0.5,
                low=price - 0.5,
                close=price,
                volume=float(i),
            )
        )
    return out


# --------------------------------------------------------------------------
# Resampling
# --------------------------------------------------------------------------


def test_an_hour_is_the_twelve_five_minute_bars_inside_it() -> None:
    bars = _series(24)

    hourly = resample(bars, Interval.H1)

    assert len(hourly) == 2
    first = hourly[0]
    assert first.ts == START
    assert first.open == bars[0].open
    assert first.close == bars[11].close
    assert first.high == max(b.high for b in bars[:12])
    assert first.low == min(b.low for b in bars[:12])
    assert first.volume == sum(b.volume for b in bars[:12])


def test_buckets_are_aligned_to_the_clock_not_to_the_first_bar() -> None:
    """A run starting at 09:35 must not create hours that run 09:35 to 10:35."""
    bars = [
        Bar(ts=datetime(2026, 1, 1, 9, 35, tzinfo=UTC) + timedelta(minutes=5 * i),
            open=1.0, high=1.0, low=1.0, close=1.0, volume=1.0)
        for i in range(12)
    ]

    hourly = resample(bars, Interval.H1)

    assert [b.ts for b in hourly] == [
        datetime(2026, 1, 1, 9, tzinfo=UTC),
        datetime(2026, 1, 1, 10, tzinfo=UTC),
    ]


def test_an_unfinished_bucket_is_still_returned() -> None:
    """Whether it has closed depends on the moment it is read at, which is the
    view's business and not this function's."""
    hourly = resample(_series(13), Interval.H1)

    assert len(hourly) == 2
    assert bucket_for(hourly[1].ts, Interval.H1) == hourly[1].ts


def test_resampling_nothing() -> None:
    assert resample([], Interval.H1) == []


# --------------------------------------------------------------------------
# The lookahead rule
# --------------------------------------------------------------------------


def test_a_higher_timeframe_bar_is_invisible_until_it_has_closed() -> None:
    """The whole reason this layer exists.

    At every base bar, the hourly bar on offer must be one that had finished by
    the time that base bar closed. If this ever fails, every multi-timeframe
    result in the app is reading its own future.
    """
    bars = _series(300)
    view, cursors = build(bars, Interval.M5, [Interval.H1])

    for i in range(len(bars)):
        wind(view, cursors, i)
        hourly = view.frame(Interval.H1)
        if not hourly.ready:
            continue
        shown = hourly.bar
        assert shown is not None
        knowable_at = closes_at(bars[i].ts, Interval.M5)
        assert closes_at(shown.ts, Interval.H1) <= knowable_at


def test_the_hourly_bar_changes_only_on_the_hour() -> None:
    """At 10:07 the newest closed hour began at 09:00, not 10:00."""
    bars = _series(36)  # three hours of five-minute bars from midnight
    view, cursors = build(bars, Interval.M5, [Interval.H1])

    # bar 13 starts at 01:05 and closes at 01:10
    wind(view, cursors, 13)
    shown = view.frame(Interval.H1).bar
    assert shown is not None
    assert shown.ts == datetime(2026, 1, 1, 0, tzinfo=UTC)

    # bar 23 closes exactly at 02:00, which is the moment the 01:00 hour finishes
    wind(view, cursors, 23)
    shown = view.frame(Interval.H1).bar
    assert shown is not None
    assert shown.ts == datetime(2026, 1, 1, 1, tzinfo=UTC)


def test_a_higher_timeframe_has_nothing_to_say_at_first() -> None:
    """Fifty-five minutes in, no hour has closed, and the frame says so."""
    bars = _series(11)
    view, cursors = build(bars, Interval.M5, [Interval.H1])

    wind(view, cursors, 10)

    hourly = view.frame(Interval.H1)
    assert hourly.ready is False
    assert hourly.bar is None
    assert hourly.close is None
    assert hourly.ema(3) is None


def test_the_traded_timeframe_sees_the_bar_that_just_closed() -> None:
    """Unlike a higher frame: a decision is made on the close of the current bar."""
    bars = _series(20)
    view, cursors = build(bars, Interval.M5)

    wind(view, cursors, 7)

    assert view.base.bar is bars[7]
    assert view.at == closes_at(bars[7].ts, Interval.M5)


def test_a_timeframe_the_rule_did_not_declare_is_refused() -> None:
    view, _ = build(_series(20), Interval.M5, [Interval.H1])

    with pytest.raises(KeyError, match="declare its timeframes"):
        view.frame(Interval.H4)


def test_a_context_frame_shorter_than_the_traded_one_is_refused() -> None:
    with pytest.raises(ValueError, match="has to be higher"):
        build(_series(20), Interval.H1, [Interval.M5])


# --------------------------------------------------------------------------
# Indicators through a frame
# --------------------------------------------------------------------------


def test_an_indicator_read_at_the_cursor_equals_one_computed_from_the_past() -> None:
    """The equivalence the engine's speed depends on, asserted through the frame."""
    from analytics.indicators import closes as close_prices
    from analytics.indicators import ema as ema_line

    bars = _series(120)
    view, cursors = build(bars, Interval.M5)

    for i in (30, 61, 99, 119):
        wind(view, cursors, i)
        from_the_frame = view.base.ema(20)
        computed_now = ema_line(close_prices(bars[: i + 1]), 20)[-1]
        assert from_the_frame == computed_now


def test_pivots_come_from_the_period_before_the_current_one() -> None:
    """Using today's range to trade today would be reading the future."""
    bars = _series(36)
    view, cursors = build(bars, Interval.M5, [Interval.H1])
    wind(view, cursors, 30)

    hourly = view.frame(Interval.H1)
    shown = hourly.bar
    previous = hourly.ago(1)
    levels = hourly.pivots()

    assert shown is not None and previous is not None and levels is not None
    assert levels.pivot == pytest.approx(
        (previous.high + previous.low + previous.close) / 3
    )


def test_an_indicator_is_computed_once_per_frame() -> None:
    """Otherwise a run over three years is quadratic rather than linear."""
    bars = _series(200)
    view, cursors = build(bars, Interval.M5)
    frame = view.base
    computed: list[tuple[str, int, float]] = []
    original = frame._compute

    def counting(key: tuple[str, int, float]):  # type: ignore[no-untyped-def]
        computed.append(key)
        return original(key)

    frame._compute = counting  # type: ignore[method-assign]

    for i in range(50, 200):
        wind(view, cursors, i)
        frame.ema(20)
        frame.rsi(14)

    assert computed == [("ema", 20, 0.0), ("rsi", 14, 0.0)]


# --------------------------------------------------------------------------
# Against real bars
# --------------------------------------------------------------------------

_STORE = Path(__file__).resolve().parents[2] / "data" / "bars.duckdb"


@pytest.mark.skipif(not _STORE.is_file(), reason="no bar store to check against")
def test_resampled_hours_match_the_hours_the_source_served() -> None:
    """The reason the 1h series was fetched at all.

    Higher timeframes are resampled rather than fetched so they cannot disagree
    with the base series. That argument is only worth anything if the resampling
    is right, and the fetched hourly series is the independent witness.

    Skipped when the store is locked by a running desk - DuckDB allows one writer
    and will not open the file beside it, which is not a test failure.
    """
    import duckdb

    from marketdata.store import BarStore

    try:
        store = BarStore(_STORE)
    except duckdb.IOException:
        pytest.skip("the bar store is open in another process")

    try:
        base = store.read(Series("binance", "BTCUSDT", Interval.M5))
        served = store.read(Series("binance", "BTCUSDT", Interval.H1))
    finally:
        store.close()
    if len(base) < 5000 or len(served) < 500:
        pytest.skip("not enough history stored to compare")

    ours = {b.ts: b for b in resample(base, Interval.H1)}
    checked = 0
    for hour in served:
        mine = ours.get(hour.ts)
        if mine is None:
            continue
        # The last bucket of the base series is usually partial, and the first is
        # only partial if the base series began mid-hour. Both are honest
        # disagreements about coverage rather than about arithmetic.
        if hour.ts in (min(ours), max(ours)):
            continue
        assert mine.open == pytest.approx(hour.open), hour.ts
        assert mine.high == pytest.approx(hour.high), hour.ts
        assert mine.low == pytest.approx(hour.low), hour.ts
        assert mine.close == pytest.approx(hour.close), hour.ts
        checked += 1

    assert checked > 500
