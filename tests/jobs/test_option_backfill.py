"""The desk keeping the options backtest's history current after each expiry."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, date, datetime, time
from typing import Any

import pytest

import jobs.option_backfill as job
from jobs.option_backfill import OVERLAP, OptionBackfiller, in_quiet_hours
from optbt.data.backfill import ExpiryReport
from optbt.data.models import Contract, Kind
from optbt.data.store import OptionStore
from venues.calendar import IST

SATURDAY = date(2026, 10, 3)
MONDAY = date(2026, 10, 5)


def _at(day: date, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(day, time(hh, mm), tzinfo=IST).astimezone(UTC)


def _store(held: dict[str, date]) -> OptionStore:
    store = OptionStore()
    for underlying, expiry in held.items():
        contract = Contract(
            symbol=f"{underlying}-{expiry}",
            underlying=underlying,
            expiry=expiry,
            kind=Kind.CALL,
            strike=23000.0,
        )
        store.write_contract(contract, [], fetched_at=datetime(2026, 9, 29))
    return store


class _Calls:
    def __init__(self, failed: tuple[str, ...] = ()) -> None:
        self.calls: list[tuple[str, date, date]] = []
        self.failed = failed

    def __call__(
        self, store: Any, source: Any, underlying: str, *, since: date, until: date, band: float
    ) -> Iterator[ExpiryReport]:
        self.calls.append((underlying, since, until))
        yield ExpiryReport(
            expiry=date(2026, 9, 29),
            listed=10,
            wanted=8,
            already_held=0,
            fetched=8 - len(self.failed),
            bars=3000,
            failed=self.failed,
        )


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> _Calls:
    fake = _Calls()
    monkeypatch.setattr(job, "backfill", fake)
    monkeypatch.setattr(job, "fetch_for", _fetch_for)
    return fake


def _fetch_for(store: Any, source: Any, progress: Any, *, today: date, wait: bool) -> Any:
    _LIVE.append((progress.underlying, progress.day, today))
    progress.state = "done"
    return progress


_LIVE: list[tuple[str, date, date]] = []


def _job(store: OptionStore, now: datetime) -> OptionBackfiller:
    # The source is never reached: `backfill` is replaced in every test.
    return OptionBackfiller(source=lambda: None, store=lambda: store, now=lambda: now)  # type: ignore[arg-type,return-value]


def test_fetches_from_just_behind_the_newest_held_expiry_to_today(calls: _Calls) -> None:
    store = _store({"NIFTY": date(2026, 9, 22)})
    assert asyncio.run(_job(store, _at(SATURDAY, 11)).tick()) == 8
    assert calls.calls == [("NIFTY", date(2026, 9, 22) - OVERLAP, SATURDAY)]


def test_once_a_day(calls: _Calls) -> None:
    backfiller = _job(_store({"NIFTY": date(2026, 9, 22)}), _at(SATURDAY, 11))
    asyncio.run(backfiller.tick())
    asyncio.run(backfiller.tick())
    assert len(calls.calls) == 1
    assert backfiller.last_day == SATURDAY


def test_a_failed_contract_is_asked_again_the_same_day(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _Calls(failed=("NSE:NIFTY26SEP23000CE",))
    monkeypatch.setattr(job, "backfill", fake)
    backfiller = _job(_store({"NIFTY": date(2026, 9, 22)}), _at(SATURDAY, 11))
    asyncio.run(backfiller.tick())
    asyncio.run(backfiller.tick())
    assert len(fake.calls) == 2
    assert backfiller.last_day is None
    assert backfiller.last_error is not None


def test_leaves_the_session_to_the_desk(calls: _Calls) -> None:
    store = _store({"NIFTY": date(2026, 9, 22)})
    assert asyncio.run(_job(store, _at(MONDAY, 11)).tick()) == 0
    assert calls.calls == []
    asyncio.run(_job(store, _at(MONDAY, 16)).tick())
    assert len(calls.calls) == 1


def test_an_underlying_never_loaded_is_left_to_the_script(calls: _Calls) -> None:
    assert asyncio.run(_job(_store({}), _at(SATURDAY, 11)).tick()) == 0
    assert calls.calls == []


def test_quiet_hours_are_weekday_market_hours_in_ist() -> None:
    assert in_quiet_hours(_at(MONDAY, 9, 0))
    assert in_quiet_hours(_at(MONDAY, 15, 44))
    assert not in_quiet_hours(_at(MONDAY, 15, 45))
    assert not in_quiet_hours(_at(MONDAY, 8, 59))
    assert not in_quiet_hours(_at(SATURDAY, 11))


def test_refreshes_the_live_expiry_up_to_yesterday(calls: _Calls) -> None:
    _LIVE.clear()
    store = _store({"NIFTY": SATURDAY})
    asyncio.run(_job(store, _at(SATURDAY, 20)).tick())
    assert _LIVE == [("NIFTY", date(2026, 10, 2), SATURDAY)]
