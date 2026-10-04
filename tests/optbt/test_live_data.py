"""Contracts still trading: fetched up to yesterday, kept apart from the settled
ledger, read alongside it, and replaced when the expiry settles."""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from optbt.data.history import History
from optbt.data.live import Progress, fetch_for
from optbt.data.models import Candle, Contract, Kind
from optbt.data.store import OptionStore, connect
from optbt.market import OptionKey
from tests.optbt.test_option_data import FETCHED, _minutes, _option, _Source

#: Monday; the weekly trading expiry is Tuesday week.
TODAY = date(2026, 10, 5)
LIVE = date(2026, 10, 13)


def _live(strike: float, kind: Kind = Kind.CALL) -> Contract:
    return replace(
        _option(strike, expiry=LIVE, kind=kind), symbol=f"NSE:NIFTY26O13{int(strike)}{kind}"
    )


def _history(store: OptionStore) -> History:
    return History(store._held)  # type: ignore[arg-type]  # in memory: the store's own connection


def test_a_live_contract_is_read_with_the_settled_ones_and_not_ledgered() -> None:
    store = OptionStore()
    store.write_live(_live(23500), _minutes(date(2026, 10, 1)), fetched_at=FETCHED)
    assert store.held([_live(23500).symbol]) == set()
    assert store.live_through([_live(23500).symbol]) == {
        _live(23500).symbol: datetime(2026, 10, 1, 9, 17)
    }
    bars = _history(store).contract_day(OptionKey(LIVE, 23500.0, Kind.CALL), date(2026, 10, 1))
    assert len(bars) == 3


def test_a_refetch_replaces_rather_than_doubles() -> None:
    store = OptionStore()
    c = _live(23500)
    store.write_live(c, _minutes(date(2026, 10, 1)), fetched_at=FETCHED)
    store.write_live(
        c, _minutes(date(2026, 10, 1)) + _minutes(date(2026, 10, 2)), fetched_at=FETCHED
    )
    assert store._conn.execute("SELECT count(*) FROM live_bar").fetchone() == (6,)


def test_settling_replaces_the_live_rows_in_the_same_write() -> None:
    store = OptionStore()
    c = _live(23500)
    store.write_live(c, _minutes(date(2026, 10, 1)), fetched_at=FETCHED)
    store.write_contract(c, _minutes(date(2026, 10, 1), 5), fetched_at=FETCHED)
    assert store._conn.execute("SELECT count(*) FROM live_bar").fetchone() == (0,)
    assert store.live_through([c.symbol]) == {}
    # And a late live refresh of a settled contract is ignored, not doubled.
    assert store.write_live(c, _minutes(date(2026, 10, 1)), fetched_at=FETCHED) == 0
    bars = _history(store).contract_day(OptionKey(LIVE, 23500.0, Kind.CALL), date(2026, 10, 1))
    assert len(bars) == 5


def test_expiry_status_tells_settled_live_and_missing_apart() -> None:
    store = OptionStore()
    store.write_contract(_option(23500), _minutes(date(2026, 6, 15)), fetched_at=FETCHED)
    store.write_live(_live(23500), _minutes(date(2026, 10, 1)), fetched_at=FETCHED)
    # A monthly whose future alone was fetched, for the futures price.
    future = Contract("NSE:NIFTY26OCTFUT", "NIFTY", date(2026, 10, 20), Kind.FUTURE, None)
    store.write_live(future, _minutes(date(2026, 10, 1)), fetched_at=FETCHED)
    status = _history(store).expiry_status([date(2026, 6, 16), LIVE, date(2026, 10, 20)])
    assert status == {
        date(2026, 6, 16): ("held", None),
        LIVE: ("live", datetime(2026, 10, 1, 9, 17)),
        date(2026, 10, 20): ("missing", None),
    }


class _LiveSource(_Source):
    interval = 0.0

    def __init__(self, contracts: list[Contract], listed_on: date, live: list[tuple[date, bool]]):
        super().__init__(contracts, listed_on)
        self._live = live

    def live_expiries(self, underlying: str) -> list[tuple[date, bool]]:
        return self._live

    def index_candles(self, symbol: str, resolution: str, start: date, end: date) -> list[Candle]:
        return []


def test_a_day_with_nothing_held_fetches_the_nearest_trading_expiry_up_to_yesterday() -> None:
    contracts = [_live(23500), _live(23500, Kind.PUT)]
    source = _LiveSource(
        contracts, listed_on=date(2026, 9, 29), live=[(date(2026, 10, 1), False), (LIVE, False)]
    )
    store = OptionStore()
    day = date(2026, 10, 1)
    p = fetch_for(store, source, Progress("NIFTY", day, None), today=TODAY, now=lambda: FETCHED)
    assert (p.state, p.expiry, p.total, p.done, p.failed) == ("done", LIVE, 2, 2, [])
    # Up to Sunday the 4th, never into today - and kept out of the ledger.
    assert {end for _, _, end in source.asked} == {TODAY - timedelta(days=1)}
    assert store.held([c.symbol for c in contracts]) == set()
    assert LIVE in store.expiries("NIFTY")
    assert date(2026, 10, 1) not in store.expiries("NIFTY")  # already past: not trading


def test_an_old_settled_expiry_is_fetched_whole_into_the_ledger() -> None:
    """A day in June, asked about in October: the expiry settled long ago, so it
    is fetched to its end and ledgered like any the backfill wrote."""
    contracts = [_option(23500), _option(23500, kind=Kind.PUT)]
    source = _LiveSource(contracts, listed_on=date(2026, 6, 1), live=[])
    store = OptionStore()
    p = fetch_for(
        store, source, Progress("NIFTY", date(2026, 6, 15), None), today=TODAY, now=lambda: FETCHED
    )
    assert (p.state, p.expiry, p.done) == ("done", date(2026, 6, 16), 2)
    assert {end for _, _, end in source.asked} == {date(2026, 6, 16)}
    assert store.held([c.symbol for c in contracts]) == {c.symbol for c in contracts}


def test_a_reader_and_a_writer_in_one_process_wait_for_each_other(tmp_path: Path) -> None:
    """DuckDB refuses them as "a different configuration" rather than a lock;
    `connect` waits that out the same way."""
    path = str(tmp_path / "options.duckdb")
    OptionStore(path)
    reader = connect(path, read_only=True)
    done: list[Any] = []

    def write() -> None:
        OptionStore(path).write_live(_live(23500), _minutes(date(2026, 10, 1)), fetched_at=FETCHED)
        done.append(True)

    writer = threading.Thread(target=write)
    writer.start()
    writer.join(0.3)
    assert not done  # still waiting for the reader
    reader.close()
    writer.join(5)
    assert done
