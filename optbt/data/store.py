"""Option history on disk.

DuckDB, in its own file rather than beside the desk's bars. DuckDB allows one
writer per file and the running desk holds `bars.duckdb`; a backfill that runs for
hours should not have to stop the app, nor the app wait on the backfill.

The shape differs from `marketdata.store` on purpose, because the size does. Four
years of NIFTY contracts is a few hundred million rows, and a primary key on that
is an index DuckDB keeps in memory and consults on every insert. So `option_bar`
has none, and duplicates are prevented one level up:

    An expired contract never changes. Once its bars are written, it is written.
    `contract` records that, in the same transaction as the bars - so a contract
    is either wholly held and ledgered, or absent and fetched again. There is no
    state in which half of one is stored, and nothing to deduplicate.

Rows are written an expiry at a time, so DuckDB's per-block min/max lets "every
strike of this expiry at 10:15" read a small slice of the file rather than all
of it.

Index bars are few (375 a day) and are topped up at the live edge, so they keep a
primary key and replace on conflict, as the desk's store does.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Callable, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any, cast

import duckdb

from optbt.data.models import Candle, Contract

SCHEMA = """
CREATE TABLE IF NOT EXISTS expiry (
    underlying VARCHAR NOT NULL,
    expiry     DATE    NOT NULL,
    -- 'options' or 'futures'. A monthly is both; a weekly is options only.
    kind       VARCHAR NOT NULL,
    PRIMARY KEY (underlying, expiry, kind)
);

-- The ledger. A row here means the contract's bars are in option_bar, all of them.
CREATE TABLE IF NOT EXISTS contract (
    symbol     VARCHAR   PRIMARY KEY,
    underlying VARCHAR   NOT NULL,
    expiry     DATE      NOT NULL,
    kind       VARCHAR   NOT NULL,   -- CE, PE or FUT
    strike     DOUBLE,               -- NULL for a future
    bars       INTEGER   NOT NULL,   -- 0: listed, and never traded in the window asked
    first_ts   TIMESTAMP,
    last_ts    TIMESTAMP,
    fetched_at TIMESTAMP NOT NULL
);

-- One minute of one contract. IST, naive: see models.IST.
-- A minute with volume 0 did not trade, and its price is the last one repeated.
CREATE TABLE IF NOT EXISTS option_bar (
    underlying VARCHAR   NOT NULL,
    expiry     DATE      NOT NULL,
    kind       VARCHAR   NOT NULL,
    strike     DOUBLE,
    ts         TIMESTAMP NOT NULL,
    open       DOUBLE    NOT NULL,
    high       DOUBLE    NOT NULL,
    low        DOUBLE    NOT NULL,
    close      DOUBLE    NOT NULL,
    volume     BIGINT    NOT NULL,
    oi         BIGINT    NOT NULL
);

-- Contracts still trading, fetched so far: kept apart from the ledger above, which
-- holds a contract only once its every bar is in. When the expiry settles and the
-- backfill writes a contract whole, its rows here go in the same transaction.
CREATE TABLE IF NOT EXISTS live_contract (
    symbol     VARCHAR   PRIMARY KEY,
    underlying VARCHAR   NOT NULL,
    expiry     DATE      NOT NULL,
    kind       VARCHAR   NOT NULL,
    strike     DOUBLE,
    bars       INTEGER   NOT NULL,
    -- The last bar held: the contract is known up to here and no further.
    last_ts    TIMESTAMP,
    fetched_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS live_bar (
    underlying VARCHAR   NOT NULL,
    expiry     DATE      NOT NULL,
    kind       VARCHAR   NOT NULL,
    strike     DOUBLE,
    ts         TIMESTAMP NOT NULL,
    open       DOUBLE    NOT NULL,
    high       DOUBLE    NOT NULL,
    low        DOUBLE    NOT NULL,
    close      DOUBLE    NOT NULL,
    volume     BIGINT    NOT NULL,
    oi         BIGINT    NOT NULL
);

CREATE TABLE IF NOT EXISTS index_bar (
    symbol     VARCHAR   NOT NULL,
    resolution VARCHAR   NOT NULL,
    ts         TIMESTAMP NOT NULL,
    open       DOUBLE    NOT NULL,
    high       DOUBLE    NOT NULL,
    low        DOUBLE    NOT NULL,
    close      DOUBLE    NOT NULL,
    volume     BIGINT    NOT NULL,
    PRIMARY KEY (symbol, resolution, ts)
);
"""


def _values(candles: Sequence[Candle]) -> str:
    # Numbers and timestamps written into the SQL, as marketdata.store does and for
    # its reason: binding is the bottleneck, by two orders of magnitude. Only
    # machine-produced values go in this way; anything naming a contract is bound.
    return ",".join(
        f"(TIMESTAMP '{c.ts.isoformat(sep=' ')}',{c.open!r},{c.high!r},{c.low!r},"
        f"{c.close!r},{c.volume},{c.oi})"
        for c in candles
    )


#: How long to wait for another process to let go of the file.
LOCK_WAIT = 120.0


def connect(path: str, *, read_only: bool, wait: float = LOCK_WAIT) -> duckdb.DuckDBPyConnection:
    """Open the store, waiting for another process to finish with it.

    DuckDB lets one process in at a time when any of them writes. Writers here
    hold the file only for the moment of a write and readers only for one run,
    so a conflict is short, and the right answer to one is to wait - not to fail
    a backtest because a backfill happened to be writing a contract.
    """
    deadline = time.monotonic() + wait
    delay = 0.05
    while True:
        try:
            return duckdb.connect(path, read_only=read_only)
        except (duckdb.IOException, duckdb.ConnectionException) as exc:
            # Another process's lock, or - inside one process - a reader and a
            # writer at once, which DuckDB refuses as "a different configuration"
            # rather than as a lock. Both pass as soon as the other lets go.
            text = str(exc).lower()
            busy = "lock" in text or "different configuration" in text
            if not busy or time.monotonic() >= deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 1.0)


def _sessioned[F: Callable[..., Any]](method: F) -> F:
    """Hold the file open for this call only, so readers get it in between."""

    @functools.wraps(method)
    def wrapper(self: OptionStore, *args: Any, **kwargs: Any) -> Any:
        if self._held is not None:
            return method(self, *args, **kwargs)
        self._held = connect(self._path, read_only=False)
        try:
            return method(self, *args, **kwargs)
        finally:
            self._held.close()
            self._held = None

    return cast(F, wrapper)


class OptionStore:
    """The store, opened for each operation rather than for the whole run.

    A backfill used to hold the file from start to finish - six hours for the
    first one, two minutes for a weekly top-up or a dry run - and every backtest
    in that time was refused. Opening costs about 4ms, so each write now takes
    the file and gives it straight back. In memory (for tests) it stays open,
    since a closed in-memory database is gone.
    """

    def __init__(self, path: Path | str = ":memory:") -> None:
        self._path = str(path)
        self._memory = self._path == ":memory:"
        self._held: duckdb.DuckDBPyConnection | None = None
        if self._memory:
            self._held = duckdb.connect(self._path)
            self._held.execute(SCHEMA)
        else:
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
            with connect(self._path, read_only=False) as conn:
                conn.execute(SCHEMA)

    @property
    def _conn(self) -> duckdb.DuckDBPyConnection:
        assert self._held is not None, "store used outside a session"
        return self._held

    def close(self) -> None:
        if self._memory and self._held is not None:
            self._held.close()
            self._held = None

    # ---------------------------------------------------------------- expiries

    @_sessioned
    def write_expiries(self, underlying: str, kind: str, expiries: Sequence[date]) -> None:
        for expiry in expiries:
            self._conn.execute(
                "INSERT OR IGNORE INTO expiry VALUES (?, ?, ?)", [underlying, expiry, kind]
            )

    @_sessioned
    def expiries(self, underlying: str, kind: str = "options") -> list[date]:
        rows = self._conn.execute(
            "SELECT expiry FROM expiry WHERE underlying = ? AND kind = ? ORDER BY expiry",
            [underlying, kind],
        ).fetchall()
        return [row[0] for row in rows]

    # --------------------------------------------------------------- contracts

    @_sessioned
    def held(self, symbols: Sequence[str]) -> set[str]:
        """Which of these contracts are already fully stored."""
        if not symbols:
            return set()
        rows = self._conn.execute(
            "SELECT symbol FROM contract WHERE symbol IN (SELECT unnest(?))", [list(symbols)]
        ).fetchall()
        return {row[0] for row in rows}

    @_sessioned
    def write_contract(
        self, contract: Contract, candles: Sequence[Candle], *, fetched_at: datetime
    ) -> int:
        """A contract's bars and its ledger row, together or not at all.

        Refuses a contract already held. The backfill never asks, so reaching that
        is a bug, and a loud one is better than a quiet doubling.
        """
        latest = {c.ts: c for c in candles}
        ordered = [latest[ts] for ts in sorted(latest)]
        self._conn.execute("BEGIN TRANSACTION")
        try:
            # The primary key on `contract` is what rejects a second write.
            self._conn.execute(
                "INSERT INTO contract VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    contract.symbol,
                    contract.underlying,
                    contract.expiry,
                    str(contract.kind),
                    contract.strike,
                    len(ordered),
                    ordered[0].ts if ordered else None,
                    ordered[-1].ts if ordered else None,
                    fetched_at,
                ],
            )
            if ordered:
                self._conn.execute(
                    "INSERT INTO option_bar SELECT ?, ?, ?, ?, ts, open, high, low, close, "
                    f"volume, oi FROM (VALUES {_values(ordered)}) "
                    "AS incoming(ts, open, high, low, close, volume, oi)",
                    [contract.underlying, contract.expiry, str(contract.kind), contract.strike],
                )
            # Held whole now: what was fetched of it while it traded goes.
            self._drop_live(contract)
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        return len(ordered)

    # -------------------------------------------------------------------- live

    def _drop_live(self, contract: Contract) -> None:
        self._conn.execute(
            "DELETE FROM live_bar WHERE underlying = ? AND expiry = ? AND kind = ? "
            "AND strike IS NOT DISTINCT FROM ?",
            [contract.underlying, contract.expiry, str(contract.kind), contract.strike],
        )
        self._conn.execute("DELETE FROM live_contract WHERE symbol = ?", [contract.symbol])

    @_sessioned
    def write_live(
        self, contract: Contract, candles: Sequence[Candle], *, fetched_at: datetime
    ) -> int:
        """A trading contract's bars so far, replacing what was held of it.

        Whole rather than appended: a refresh asks for the contract from its
        listing again - one request for a weekly - so there is no seam between
        two fetches to get wrong. A contract already held whole is left alone.
        """
        if self.held([contract.symbol]):
            return 0
        latest = {c.ts: c for c in candles}
        ordered = [latest[ts] for ts in sorted(latest)]
        self._conn.execute("BEGIN TRANSACTION")
        try:
            self._drop_live(contract)
            self._conn.execute(
                "INSERT INTO live_contract VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    contract.symbol,
                    contract.underlying,
                    contract.expiry,
                    str(contract.kind),
                    contract.strike,
                    len(ordered),
                    ordered[-1].ts if ordered else None,
                    fetched_at,
                ],
            )
            if ordered:
                self._conn.execute(
                    "INSERT INTO live_bar SELECT ?, ?, ?, ?, ts, open, high, low, close, "
                    f"volume, oi FROM (VALUES {_values(ordered)}) "
                    "AS incoming(ts, open, high, low, close, volume, oi)",
                    [contract.underlying, contract.expiry, str(contract.kind), contract.strike],
                )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        return len(ordered)

    @_sessioned
    def live_through(self, symbols: Sequence[str]) -> dict[str, datetime | None]:
        """Of these trading contracts, the ones fetched, and their last bar held."""
        if not symbols:
            return {}
        rows = self._conn.execute(
            "SELECT symbol, last_ts FROM live_contract WHERE symbol IN (SELECT unnest(?))",
            [list(symbols)],
        ).fetchall()
        return {row[0]: row[1] for row in rows}

    # ------------------------------------------------------------------ index

    @_sessioned
    def write_index(self, symbol: str, resolution: str, candles: Sequence[Candle]) -> int:
        if not candles:
            return 0
        latest = {c.ts: c for c in candles}
        values = ",".join(
            f"(TIMESTAMP '{c.ts.isoformat(sep=' ')}',{c.open!r},{c.high!r},{c.low!r},"
            f"{c.close!r},{c.volume})"
            for c in latest.values()
        )
        self._conn.execute(
            "INSERT OR REPLACE INTO index_bar SELECT ?, ?, ts, open, high, low, close, volume "
            f"FROM (VALUES {values}) AS incoming(ts, open, high, low, close, volume)",
            [symbol, resolution],
        )
        return len(latest)

    @_sessioned
    def index_range(self, symbol: str, resolution: str) -> tuple[datetime, datetime] | None:
        row = self._conn.execute(
            "SELECT min(ts), max(ts) FROM index_bar WHERE symbol = ? AND resolution = ?",
            [symbol, resolution],
        ).fetchone()
        if row is None or row[0] is None:
            return None
        return row[0], row[1]

    @_sessioned
    def daily_ranges(self, symbol: str) -> dict[date, tuple[float, float]]:
        """Low and high per day, from whatever resolution of the index is held."""
        rows = self._conn.execute(
            "SELECT CAST(ts AS DATE) AS d, min(low), max(high) FROM index_bar "
            "WHERE symbol = ? GROUP BY d",
            [symbol],
        ).fetchall()
        return {row[0]: (float(row[1]), float(row[2])) for row in rows}

    # ------------------------------------------------------------------ report

    @_sessioned
    def held_through(self) -> dict[str, date]:
        """Per underlying, the newest expiry with any contract held."""
        rows = self._conn.execute(
            "SELECT underlying, max(expiry) FROM contract GROUP BY underlying"
        ).fetchall()
        return {str(row[0]): row[1] for row in rows}

    @_sessioned
    def summary(self) -> list[tuple[str, int, int, int]]:
        """Per underlying: contracts held, bars held, expiries held."""
        return [
            (str(r[0]), int(r[1]), int(r[2]), int(r[3]))
            for r in self._conn.execute(
                "SELECT underlying, count(*), coalesce(sum(bars), 0), count(DISTINCT expiry) "
                "FROM contract GROUP BY underlying ORDER BY underlying"
            ).fetchall()
        ]
