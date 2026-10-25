"""The desk keeping its stored daily bars current after each close."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, time, timedelta

import pytest

from jobs.daily_bars import DailyBarUpdater, latest_final_day
from marketdata.models import Bar, Interval, Series
from marketdata.store import BarStore
from venues.calendar import IST

NIFTY = "NSE:NIFTY50-INDEX"
BANK = "NSE:NIFTYBANK-INDEX"


def _at(day: date, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(day, time(hh, mm), tzinfo=IST).astimezone(UTC)


def _bar(day: date, close: float = 100.0) -> Bar:
    # Stamped the way Fyers stamps a daily bar: midnight IST.
    ts = datetime.combine(day, time(0, 0), tzinfo=IST).astimezone(UTC)
    return Bar(ts=ts, open=close, high=close + 1, low=close - 1, close=close, volume=1.0)


def _sessions(start: date, end: date) -> list[date]:
    out, day = [], start
    while day <= end:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


class _Fyers:
    def __init__(self, fail: set[str] | None = None, empty: set[str] | None = None) -> None:
        self.asked: list[tuple[str, date, date]] = []
        self.fail = fail or set()
        self.empty = empty or set()

    def __call__(self, symbol: str, start: date, end: date) -> list[Bar]:
        self.asked.append((symbol, start, end))
        if symbol in self.fail:
            raise RuntimeError("-16 Could not authenticate the user")
        if symbol in self.empty:
            return []
        return [_bar(d) for d in _sessions(start, end)]


def _updater(
    store: BarStore, fyers: _Fyers, now: datetime, symbols: tuple[str, ...] = (NIFTY, BANK)
) -> DailyBarUpdater:
    return DailyBarUpdater(
        symbols=lambda: symbols,
        fetch=fyers,
        store=lambda: store,
        now=lambda: now,
        sleep=lambda _: None,
    )


@pytest.fixture
def store() -> BarStore:
    s = BarStore()
    for symbol in (NIFTY, BANK):
        held = [_bar(date(2026, 9, 24)), _bar(date(2026, 9, 25))]
        s.write(Series("fyers", symbol, Interval.D1), held)
    return s


class TestWhichDayIsFinal:
    def test_before_the_close_it_is_the_previous_session(self) -> None:
        assert latest_final_day(_at(date(2026, 9, 30), 15, 0)) == date(2026, 9, 29)

    def test_after_the_close_it_is_today(self) -> None:
        assert latest_final_day(_at(date(2026, 9, 30), 15, 45)) == date(2026, 9, 30)

    def test_a_weekend_reads_as_friday(self) -> None:
        assert latest_final_day(_at(date(2026, 9, 27), 12)) == date(2026, 9, 25)

    def test_a_holiday_is_skipped(self) -> None:
        holidays = frozenset({date(2026, 10, 2)})
        assert latest_final_day(_at(date(2026, 10, 2), 18), holidays) == date(2026, 10, 1)


def test_only_the_missing_days_are_asked_for(store: BarStore) -> None:
    fyers = _Fyers()
    written = asyncio.run(_updater(store, fyers, _at(date(2026, 9, 30), 16)).tick())

    assert fyers.asked == [
        (NIFTY, date(2026, 9, 26), date(2026, 9, 30)),
        (BANK, date(2026, 9, 26), date(2026, 9, 30)),
    ]
    assert written == 6  # 28, 29 and 30 Sep for each; the 26th and 27th are a weekend
    held = store.latest("fyers", Interval.D1)
    assert held[NIFTY].astimezone(IST).date() == date(2026, 9, 30)


def test_a_session_still_trading_is_not_fetched(store: BarStore) -> None:
    fyers = _Fyers()
    asyncio.run(_updater(store, fyers, _at(date(2026, 9, 30), 11)).tick())
    assert {end for _, _, end in fyers.asked} == {date(2026, 9, 29)}


def test_a_current_store_costs_no_requests(store: BarStore) -> None:
    fyers = _Fyers()
    up = _updater(store, fyers, _at(date(2026, 9, 30), 16))
    asyncio.run(up.tick())
    fyers.asked.clear()

    assert asyncio.run(up.tick()) == 0
    assert fyers.asked == []
    assert up.last_day == date(2026, 9, 30)


def test_a_symbol_that_fails_is_asked_again_and_the_rest_still_land(store: BarStore) -> None:
    fyers = _Fyers(fail={BANK})
    up = _updater(store, fyers, _at(date(2026, 9, 30), 16))
    asyncio.run(up.tick())

    held = store.latest("fyers", Interval.D1)
    assert held[NIFTY].astimezone(IST).date() == date(2026, 9, 30)
    assert up.last_error is not None and "1 symbols failed" in up.last_error
    assert up.last_day is None

    fyers.fail.clear()
    fyers.asked.clear()
    asyncio.run(up.tick())
    assert [s for s, _, _ in fyers.asked] == [BANK]
    assert up.last_error is None


def test_a_symbol_with_no_bars_is_asked_once_a_session(store: BarStore) -> None:
    fyers = _Fyers(empty={BANK})
    up = _updater(store, fyers, _at(date(2026, 9, 30), 16))
    asyncio.run(up.tick())
    fyers.asked.clear()

    asyncio.run(up.tick())
    assert fyers.asked == []


def test_a_new_member_gets_a_year(store: BarStore) -> None:
    fyers = _Fyers()
    new = "NSE:BSE-EQ"
    asyncio.run(_updater(store, fyers, _at(date(2026, 9, 30), 16), symbols=(new,)).tick())
    assert fyers.asked[0][1] == date(2026, 9, 30) - timedelta(days=360)


def test_a_store_held_elsewhere_is_an_error_not_a_crash() -> None:
    up = DailyBarUpdater(
        symbols=lambda: (NIFTY,), fetch=_Fyers(), store=lambda: None, sleep=lambda _: None
    )
    with pytest.raises(RuntimeError, match="another process"):
        asyncio.run(up.tick())


def test_a_bar_stored_mid_session_is_replaced_by_the_close(store: BarStore) -> None:
    """The chart stores the underlyings live, so a day's bar can be its candle so
    far. Fetched before 15:45 that day, it is asked for again once final."""
    day = date(2026, 9, 29)
    series = Series("fyers", NIFTY, Interval.D1)
    store.write(series, [_bar(date(2026, 9, 28)), _bar(day, close=90.0)])
    store.note_fetch(series, ok=True, at=_at(day, 11, 19))

    fyers = _Fyers()
    asyncio.run(_updater(store, fyers, _at(date(2026, 9, 30), 11), symbols=(NIFTY,)).tick())

    assert fyers.asked == [(NIFTY, day, day)]
    assert store.read(series)[-1].close == 100.0


def test_a_bar_fetched_after_the_close_is_trusted(store: BarStore) -> None:
    day = date(2026, 9, 29)
    series = Series("fyers", NIFTY, Interval.D1)
    store.write(series, [_bar(date(2026, 9, 28)), _bar(day)])
    store.note_fetch(series, ok=True, at=_at(day, 16))

    fyers = _Fyers()
    asyncio.run(_updater(store, fyers, _at(date(2026, 9, 30), 11), symbols=(NIFTY,)).tick())
    assert fyers.asked == []


def test_todays_candle_so_far_is_left_alone(store: BarStore) -> None:
    today = date(2026, 9, 30)
    series = Series("fyers", NIFTY, Interval.D1)
    store.write(series, [_bar(date(2026, 9, 29)), _bar(today)])
    store.note_fetch(series, ok=True, at=_at(today, 11))

    fyers = _Fyers()
    asyncio.run(_updater(store, fyers, _at(today, 11, 30), symbols=(NIFTY,)).tick())
    assert fyers.asked == []


def test_a_rate_limit_is_waited_out_once() -> None:
    from broker.errors import RateLimited

    calls: list[str] = []

    def fetch(symbol: str, start: date, end: date) -> list[Bar]:
        calls.append(symbol)
        if len(calls) == 1:
            raise RateLimited("request limit reached")
        return [_bar(end)]

    store = BarStore()
    up = DailyBarUpdater(
        symbols=lambda: (NIFTY,),
        fetch=fetch,
        store=lambda: store,
        now=lambda: _at(date(2026, 9, 30), 16),
        sleep=lambda _: None,
    )
    asyncio.run(up.tick())
    assert calls == [NIFTY, NIFTY]
    assert up.last_error is None
