"""Writing down NSE's pre-open auction every session, without being asked.

NSE serves one session's pre-open - the latest - until the next morning's order
collection replaces it. A day missed is a day lost, so this runs inside the
desk, as the volatility recorder does, rather than depending on somebody
remembering a script.

It asks only when there is something to get: the latest session whose auction
has settled (09:08 IST) and is not stored yet. A desk started at 08:30 still
catches the previous day, which NSE is still showing; a desk started at noon
still catches the morning's. Once the day is in, a pass costs nothing.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

from marketdata.nse_preopen import DEFAULT_KEYS, SETTLED_AT, PreOpenDay
from storage.preopen_repo import save_day
from venues.calendar import IST

log = logging.getLogger(__name__)

#: Between passes. Only a pass with a day outstanding makes a request, so this
#: is how soon after 09:08 the morning's auction is in, not a polling rate.
DEFAULT_INTERVAL = 600.0


def latest_settled_day(at: datetime, holidays: frozenset[date] = frozenset()) -> date:
    """The most recent session whose pre-open had settled by `at`."""
    local = at.astimezone(IST)
    day = local.date()
    if local.time() < SETTLED_AT:
        day -= timedelta(days=1)
    while day.weekday() >= 5 or day in holidays:
        day -= timedelta(days=1)
    return day


class PreOpenRecorder:
    def __init__(
        self,
        fetch: Callable[[tuple[str, ...]], PreOpenDay],
        open_conn: Callable[[], sqlite3.Connection],
        holidays: Callable[[], frozenset[date]] = lambda: frozenset(),
        keys: tuple[str, ...] = DEFAULT_KEYS,
        interval: float = DEFAULT_INTERVAL,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._fetch = fetch
        self._open_conn = open_conn
        self._holidays = holidays
        self._keys = keys
        self._interval = interval
        self._now = now
        self.last_error: str | None = None
        self.last_day: date | None = None

    async def run_forever(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a bad pass must not end the loop
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("pre-open recording failed: %s", self.last_error)
            await asyncio.sleep(self._interval)

    async def tick(self) -> bool:
        """One pass. Returns whether a day was written."""
        # On a thread, holidays included: the list is fetched when its cache runs out.
        return await asyncio.to_thread(self._record)

    def _record(self) -> bool:
        wanted = latest_settled_day(self._now(), self._holidays())
        conn = self._open_conn()
        try:
            have = conn.execute(
                "SELECT 1 FROM preopen_day WHERE day = ? AND source = 'nse'",
                (wanted.isoformat(),),
            ).fetchone()
            if have is not None:
                return False
            session = self._fetch(self._keys)
            result = save_day(conn, session)
        finally:
            conn.close()
        self.last_error = None
        if result.written:
            self.last_day = result.day
            log.info(
                "pre-open for %s recorded: %s, %d stocks", result.day, result.outcome, result.rows
            )
        return result.written
