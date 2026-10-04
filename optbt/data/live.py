"""Option history fetched when it is asked for.

The backfill keeps one underlying's settled expiries; anything else - another
index, a day before the store starts, an expiry still trading - was simply not
there. Fyers serves all of it from the endpoints the backfill already uses, so
when the simulator needs a day or an expiry the store lacks, it is fetched there
and then:

  - the index and VIX around the day, so the clock has the session at all;
  - the expiry calendar around it (and, near today, the expiries still trading);
  - the expiry's contracts within the backfill's strike band, and the nearest
    monthly future for the futures price.

A settled contract is written to the ledger like any the backfill fetched - it
never changes again. One still trading is fetched up to yesterday and kept apart
(see `optbt/data/store.py`) until it settles and the backfill replaces it.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Hashable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Literal

from optbt.data.backfill import (
    BAND_LOOKBACK,
    EARLIEST,
    fetch_contract,
    in_band,
    refresh_calendar,
)
from optbt.data.models import Kind
from optbt.data.source import MAX_SPAN_DAYS, LiveSource, SourceError
from optbt.data.store import OptionStore
from venues.instruments import INDIA_VIX, OPTION_SERIES

log = logging.getLogger(__name__)

#: Strikes kept: the backfill's band, so fetched and backfilled expiries look alike.
BAND = 0.10

#: Requests a minute while the market is open, out of Fyers' 200: the desk's
#: own prices come first. Out of hours the client's normal pace applies.
MARKET_HOURS_PER_MINUTE = 60

#: Index fetched past the day asked about, so stepping on through the next
#: weeks - and the expiry the day trades - does not stop at its edge.
AHEAD = timedelta(days=45)

#: Expiries listed this far past the day: the nearest weeklies and the monthly.
CALENDAR_AHEAD = timedelta(days=120)


@dataclass
class Progress:
    underlying: str
    #: The day the simulator asked about.
    day: date
    expiry: date | None
    state: Literal["index", "listing", "fetching", "done", "failed"] = "index"
    total: int = 0
    done: int = 0
    bars: int = 0
    failed: list[str] = field(default_factory=list)
    error: str | None = None
    #: The last day fetched up to.
    through: date | None = None
    started_at: datetime = field(default_factory=datetime.now)
    finished_at: datetime | None = None

    @property
    def running(self) -> bool:
        return self.state not in ("done", "failed")


def fetch_index(store: OptionStore, source: LiveSource, symbol: str, start: date, end: date) -> int:
    """One-minute bars for a span, a request per hundred days, replacing any held."""
    written = 0
    while start <= end:
        stop = min(end, start + timedelta(days=MAX_SPAN_DAYS))
        written += store.write_index(symbol, "1", source.index_candles(symbol, "1", start, stop))
        start = stop + timedelta(days=1)
    return written


def fetch_for(
    store: OptionStore,
    source: LiveSource,
    progress: Progress,
    *,
    today: date,
    band: float = BAND,
    now: Callable[[], datetime] = datetime.now,
) -> Progress:
    """What the simulator needs for `progress.day`: the session, the calendar,
    and one expiry's contracts - `progress.expiry`, or the nearest on or after it."""
    underlying, day = progress.underlying, progress.day
    yesterday = today - timedelta(days=1)
    index = OPTION_SERIES[underlying]
    try:
        if day not in store.daily_ranges(index):
            start, end = max(EARLIEST, day - BAND_LOOKBACK), min(day + AHEAD, yesterday)
            for symbol in (index, INDIA_VIX):
                fetch_index(store, source, symbol, start, end)

        progress.state = "listing"
        if day <= yesterday:
            refresh_calendar(
                store, source, underlying, day - timedelta(days=7), min(day + CALENDAR_AHEAD, today)
            )
        if day + CALENDAR_AHEAD >= today:
            live = [(e, m) for e, m in source.live_expiries(underlying) if e >= today]
            store.write_expiries(underlying, "options", [e for e, _ in live])
            store.write_expiries(underlying, "futures", [e for e, m in live if m])
        if progress.expiry is None:
            ahead = [e for e in store.expiries(underlying) if e >= day]
            if not ahead:
                raise SourceError(f"no {underlying} expiry is listed on or after {day}")
            progress.expiry = ahead[0]
        expiry = progress.expiry

        progress.state = "fetching"
        listed = source.contracts(underlying, expiry)
        wanted = in_band(listed, expiry, store.daily_ranges(index), band)
        monthly = next((e for e in store.expiries(underlying, "futures") if e >= day), None)
        if monthly is not None:
            # The futures price on the page is the nearest monthly's.
            futures = listed if monthly == expiry else source.contracts(underlying, monthly)
            wanted += [c for c in futures if c.kind is Kind.FUTURE and c not in wanted]
        held = store.held([c.symbol for c in wanted])
        todo = [c for c in wanted if c.symbol not in held]
        progress.total = len(todo)
        progress.through = min(expiry, yesterday)
        for contract in todo:
            try:
                trading = contract.expiry >= today
                candles = fetch_contract(source, contract, yesterday if trading else None)
            except SourceError as exc:
                log.warning("%s: %s", contract.symbol, exc)
                progress.failed.append(contract.symbol)
            else:
                write = store.write_live if trading else store.write_contract
                progress.bars += write(contract, candles, fetched_at=now())
            progress.done += 1
        progress.state = "done"
    except Exception as exc:  # noqa: BLE001 - reported to the page, not raised into a thread
        log.warning("fetch of %s %s %s failed: %s", underlying, day, progress.expiry, exc)
        progress.state = "failed"
        progress.error = f"{type(exc).__name__}: {exc}"
    progress.finished_at = now()
    return progress


class Fetcher:
    """One fetch at a time, on a thread, with its progress readable meanwhile.

    Each thing asked for is asked for once per process: a day that turns out to
    be a holiday, or an expiry Fyers has nothing for, is not fetched again on
    every step the page takes past it.
    """

    def __init__(
        self,
        source: Callable[[], LiveSource],
        store: Callable[[], OptionStore],
        market_open: Callable[[], bool],
        today: Callable[[], date],
    ) -> None:
        self._source = source
        self._store = store
        self._market_open = market_open
        self._today = today
        self._lock = threading.Lock()
        self._asked: set[Hashable] = set()
        self.progress: Progress | None = None

    def asked(self, key: Hashable) -> bool:
        return key in self._asked

    @property
    def running(self) -> Progress | None:
        p = self.progress
        return p if p is not None and p.running else None

    def start(self, key: Hashable, underlying: str, day: date, expiry: date | None) -> Progress:
        """Start a fetch, or return the one already running."""
        with self._lock:
            if self.progress is not None and self.progress.running:
                return self.progress
            self._asked.add(key)
            progress = Progress(underlying=underlying, day=day, expiry=expiry)
            self.progress = progress

        def run() -> None:
            try:
                source = self._source()
                if self._market_open():
                    source.interval = max(source.interval, 60 / MARKET_HOURS_PER_MINUTE)
                fetch_for(self._store(), source, progress, today=self._today())
            except Exception as exc:  # noqa: BLE001 - building the source can fail too
                progress.state = "failed"
                progress.error = f"{type(exc).__name__}: {exc}"
                progress.finished_at = datetime.now()

        threading.Thread(target=run, name="option-fetch", daemon=True).start()
        return progress
