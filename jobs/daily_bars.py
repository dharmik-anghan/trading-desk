"""Keeping the stored daily bars current, without being asked.

The rotation graph and the volatility ranks read daily bars from the store and
nothing else, and the only thing that wrote them was `scripts/backfill_nse.py` -
run by hand, with the desk stopped, because DuckDB lets one process hold the
file. So the graph sat at whatever day that was last done.

This runs inside the desk, which already holds the store, so it can write where
the script could not. After the close it asks Fyers for each symbol's missing
days - from its last stored bar to the latest finished session - and nothing
else. Once every symbol has the day, a pass is one query against the store.

The backfill script stays for the first load of years; this only catches up.
"""

from __future__ import annotations

import asyncio
import logging
import time as clock
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, time, timedelta

from broker.errors import RateLimited
from marketdata.models import Bar, Interval, Series
from marketdata.store import BarStore
from venues.calendar import IST
from venues.instruments import NSE_BARS as SOURCE

log = logging.getLogger(__name__)


#: When a session's daily bar is final. The market closes at 15:30; the margin
#: is for the closing auction and for Fyers finishing its candle.
FINAL_AT = time(15, 45)

#: Between passes. A pass with nothing outstanding reads the store and stops, so
#: this is how soon after 15:45 the day is in, not a polling rate.
DEFAULT_INTERVAL = 900.0

#: Between requests. Two hundred symbols at this pace is under a minute and a
#: third of Fyers' per-minute budget, which the desk beside it also spends.
BETWEEN = 0.3

#: How long to wait before asking once more when Fyers says too many requests.
#: The budget is per second and per minute, and the desk beside this spends it
#: too, so a short wait is usually enough.
RATE_LIMIT_WAIT = 3.0

#: How far back a symbol with nothing stored is fetched: a constituent that
#: joined an index since the backfill. Enough for a window on weekly bars.
NEW_SYMBOL_DAYS = 360


def latest_final_day(at: datetime, holidays: frozenset[date] = frozenset()) -> date:
    """The most recent session whose daily bar had finished by `at`."""
    local = at.astimezone(IST)
    day = local.date()
    if local.time() < FINAL_AT:
        day -= timedelta(days=1)
    while day.weekday() >= 5 or day in holidays:
        day -= timedelta(days=1)
    return day


def _ist_day(at: datetime) -> date:
    # Fyers stamps a daily bar at midnight IST, which is the previous day in UTC.
    return at.astimezone(IST).date()


def _final(day: date) -> datetime:
    return datetime.combine(day, FINAL_AT, tzinfo=IST)


class DailyBarUpdater:
    def __init__(
        self,
        symbols: Callable[[], Sequence[str]],
        fetch: Callable[[str, date, date], list[Bar]],
        store: Callable[[], BarStore | None],
        holidays: Callable[[], frozenset[date]] = lambda: frozenset(),
        interval: float = DEFAULT_INTERVAL,
        between: float = BETWEEN,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = clock.sleep,
    ) -> None:
        self._symbols = symbols
        self._fetch = fetch
        self._store = store
        self._holidays = holidays
        self._interval = interval
        self._between = between
        self._now = now
        self._sleep = sleep
        #: Symbols that answered nothing for a day - delisted, renamed, or not
        #: traded - so they are asked once per session rather than every pass.
        self._empty: dict[str, date] = {}
        self.last_error: str | None = None
        #: The session every symbol is current to, once a pass has got there.
        self.last_day: date | None = None

    async def run_forever(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a bad pass must not end the loop
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("daily bar update failed: %s", self.last_error)
            await asyncio.sleep(self._interval)

    async def tick(self) -> int:
        """One pass. Returns how many bars were written."""
        return await asyncio.to_thread(self._update)

    def _update(self) -> int:
        store = self._store()
        if store is None:
            raise RuntimeError("the bar store is open in another process")
        wanted = latest_final_day(self._now(), self._holidays())
        held = store.latest(SOURCE, Interval.D1)
        # The chart fetches the index underlyings live and stores what it gets,
        # so their newest bar can be a session's candle so far rather than its
        # close. One fetched before that session was final is asked for again.
        fetched = store.fetched(SOURCE, Interval.D1)

        written = 0
        failed: list[str] = []
        for symbol in self._symbols():
            last = held.get(symbol)
            if last is None:
                start = wanted - timedelta(days=NEW_SYMBOL_DAYS)
            else:
                day = _ist_day(last)
                if day > wanted:
                    continue
                at = fetched.get(symbol)
                partial = at is not None and at < _final(day)
                if day == wanted and not partial:
                    continue
                start = day if partial else day + timedelta(days=1)
            if self._empty.get(symbol) == wanted:
                continue
            try:
                bars = self._ask(symbol, start, wanted)
            except Exception as exc:  # noqa: BLE001 - one symbol costs that symbol
                failed.append(symbol)
                log.debug("daily bars for %s failed: %s", symbol, exc)
                self.last_error = f"{type(exc).__name__}: {exc}"
            else:
                series = Series(SOURCE, symbol, Interval.D1)
                if bars:
                    written += store.write(series, bars)
                    store.note_fetch(series, ok=True, note="daily update", at=self._now())
                else:
                    self._empty[symbol] = wanted
            self._sleep(self._between)

        if failed:
            # Asked again next pass; the reason is the last one seen.
            self.last_error = (
                f"{len(failed)} symbols failed ({', '.join(failed[:5])}), last: {self.last_error}"
            )
            log.warning("daily bars: %s", self.last_error)
        else:
            self.last_error = None
            self.last_day = wanted
        if written:
            log.info("daily bars to %s: %d written", wanted, written)
        return written

    def _ask(self, symbol: str, start: date, end: date) -> list[Bar]:
        try:
            return self._fetch(symbol, start, end)
        except RateLimited:
            self._sleep(RATE_LIMIT_WAIT)
            return self._fetch(symbol, start, end)
