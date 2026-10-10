"""The paper desk's watcher: stops, targets, exit-all rules and expiry, every second.

Server-side for the reason the alert watcher is: a stop that only fires while a
tab is open is not a stop, and this market trades through the night. A pass with
nothing open is one query and costs nothing; the market is only read for legs
that are open.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from api.store import open_db
from paper.desk import tick
from paper.markets import Executor, PaperMarket
from storage import paper_repo

log = logging.getLogger(__name__)

INTERVAL = 1.0


class PaperWatcher:
    def __init__(
        self,
        db_path: Path,
        markets: Callable[[], Mapping[str, PaperMarket]],
        executors: Callable[[], Mapping[str, Executor]] = dict,
        *,
        interval: float = INTERVAL,
    ) -> None:
        self._db_path = db_path
        self._markets = markets
        self._executors = executors
        self._interval = interval
        self.last_error: str | None = None

    def pass_once(self, now: datetime | None = None) -> list[str]:
        conn = open_db(self._db_path)
        try:
            if not paper_repo.open_legs(conn):
                return []
            done = tick(conn, self._markets(), now or datetime.now(UTC), self._executors())
        finally:
            conn.close()
        for line in done:
            log.info("paper: %s", line)
        return done

    async def run_forever(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.pass_once)
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a bad pass must not end the loop
                # Once a second, so a failure that persists - an expired login -
                # is logged in full when it starts, not on every pass.
                repeated = (self.last_error or "").startswith(f"{type(exc).__name__}:")
                self.last_error = f"{type(exc).__name__}: {exc}"
                if repeated:
                    log.debug("paper pass failed again: %s", exc)
                else:
                    log.exception("paper pass failed")
            await asyncio.sleep(self._interval)
