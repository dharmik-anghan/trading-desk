"""Keeping the option history current, without being asked.

The options backtest prices from `data/options.duckdb`, and only
`scripts/backfill_options.py` wrote it - by hand, after an expiry. Forget once
and every backtest stops at the last expiry somebody remembered.

This runs inside the desk and does what the script does on a rerun: fetch the
expiries that settled since the newest one held, and nothing else. An expiry is
only fetchable the day after it settles - the endpoint refuses a range reaching
today - so a pass each day picks every one up within a day of it becoming
available.

Only out of hours. A weekly expiry is a few hundred contracts, a request each,
and the desk beside this spends the same Fyers budget on prices while the market
is open.

It also refreshes the expiry still trading up to yesterday, so opening the
simulator after a few days away finds those days held rather than fetching them.

The first load of years stays the script's job: an underlying with nothing held
is left alone rather than started on a six-hour fetch at app start.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import cast

from optbt.data.backfill import backfill
from optbt.data.live import Progress, fetch_for
from optbt.data.source import ExpiredSource, LiveSource
from optbt.data.store import OptionStore
from venues.calendar import IST

log = logging.getLogger(__name__)

#: Between passes. A day's pass is done once; the rest only check the clock.
DEFAULT_INTERVAL = 3600.0

#: The window the desk keeps for its own requests: pre-open to the final candle.
QUIET_FROM = time(9, 0)
QUIET_UNTIL = time(15, 45)

#: How far behind the newest held expiry to look again. Covers a monthly that
#: settled before the newest weekly, and retries contracts that failed last time
#: - each a list request per expiry, which is cheap.
OVERLAP = timedelta(days=35)

#: The script's default strike band, so the two fill the store alike.
BAND = 0.10


def in_quiet_hours(at: datetime) -> bool:
    local = at.astimezone(IST)
    return local.weekday() < 5 and QUIET_FROM <= local.time() < QUIET_UNTIL


class OptionBackfiller:
    def __init__(
        self,
        source: Callable[[], ExpiredSource],
        store: Callable[[], OptionStore],
        underlyings: Callable[[OptionStore], Sequence[str]] | None = None,
        interval: float = DEFAULT_INTERVAL,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._source = source
        self._store = store
        self._underlyings = underlyings or (lambda s: sorted(s.held_through()))
        self._interval = interval
        self._now = now
        self.last_error: str | None = None
        #: The IST day the last complete pass ran, so a day is fetched once.
        self.last_day: date | None = None

    async def run_forever(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a bad pass must not end the loop
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("option backfill failed: %s", self.last_error)
            await asyncio.sleep(self._interval)

    async def tick(self) -> int:
        """One pass. Returns how many contracts were fetched."""
        return await asyncio.to_thread(self._update)

    def _update(self) -> int:
        at = self._now()
        today = at.astimezone(IST).date()
        if self.last_day == today or in_quiet_hours(at):
            return 0

        store = self._store()
        held = store.held_through()
        source = self._source()
        fetched = 0
        failed: list[str] = []
        for underlying in self._underlyings(store):
            newest = held.get(underlying)
            if newest is None:
                continue
            for report in backfill(
                store, source, underlying, since=newest - OVERLAP, until=today, band=BAND
            ):
                fetched += report.fetched
                failed.extend(report.failed)
                if report.fetched:
                    log.info(
                        "%s %s: %d contracts, %d bars",
                        underlying,
                        report.expiry,
                        report.fetched,
                        report.bars,
                    )

            # The expiry still trading: its days up to yesterday, which the
            # settled-only pass above cannot reach until it settles.
            progress = fetch_for(
                store,
                cast(LiveSource, source),
                Progress(underlying=underlying, day=today - timedelta(days=1), expiry=None),
                today=today,
                wait=False,
            )
            fetched += progress.done - len(progress.failed)
            if progress.state == "failed":
                failed.append(f"{underlying} live expiry: {progress.error}")
            failed.extend(progress.failed)

        if failed:
            # Not ledgered, so the next pass asks for them again.
            self.last_error = f"{len(failed)} contracts failed, e.g. {failed[:3]}"
            log.warning("option backfill: %s", self.last_error)
        else:
            self.last_error = None
            self.last_day = today
        return fetched
