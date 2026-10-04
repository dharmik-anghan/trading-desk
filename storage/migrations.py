"""Versioned schema changes.

`init_schema` creates tables with `IF NOT EXISTS`, which is enough to bring a
new database up to date and useless for changing one that already holds data:
adding a column, relaxing a constraint or renaming a table all need a
statement that runs exactly once, in order, against a database that may be
several versions behind.

The version lives in SQLite's own `user_version` pragma rather than a table of
our own. It costs no schema, it is already atomic, and it cannot drift out of
step with the file it describes.

Each migration is a numbered step with a short reason. Steps only ever get
appended - editing one that has already run somewhere means databases disagree
about what version 3 was, which is the failure mode this design exists to
avoid. To change something, add another step.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Migration:
    """One irreversible step from `version - 1` to `version`."""

    version: int
    #: Why it exists, for whoever reads the list in two years.
    reason: str
    apply: Callable[[sqlite3.Connection], None]


def _noop(conn: sqlite3.Connection) -> None:
    """Baseline. Everything `init_schema` creates is version 1 by definition."""


def _alert_state(conn: sqlite3.Connection) -> None:
    """Somewhere for the alert engine to keep its log and its active keys.

    The engine ran in the browser and kept both in `localStorage`, which is per
    origin, dies with the tab, and cannot be read by anything that sends a
    Telegram message. Moving it here is what lets alerts fire while nothing is
    open - which is the only way a 24/7 market can be watched at all.

    `alert_active` is the set of conditions currently true. It is not a log and
    has no history: it exists so that a restart does not read every still-true
    condition as a fresh transition and re-announce all of them.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS alert_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT NOT NULL,
            severity TEXT NOT NULL,
            subject TEXT,
            message TEXT NOT NULL,
            -- epoch milliseconds, matching the frontend's clock so one log can
            -- hold entries written by either engine
            at INTEGER NOT NULL,
            -- whether this one has been delivered, so a restart does not send
            -- the same Telegram message twice
            notified_at INTEGER
        );

        CREATE INDEX IF NOT EXISTS idx_alert_log_at ON alert_log(at);
        CREATE INDEX IF NOT EXISTS idx_alert_log_key_at ON alert_log(key, at);

        CREATE TABLE IF NOT EXISTS alert_active (
            key TEXT PRIMARY KEY,
            since INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS alert_limits (
            -- one row, so the thresholds are a value rather than a history
            id INTEGER PRIMARY KEY CHECK (id = 1),
            target REAL NOT NULL,
            daily_loss REAL NOT NULL,
            max_loss REAL NOT NULL,
            short_delta REAL NOT NULL,
            expiry_days REAL NOT NULL
        );
    """)


def _alert_watches(conn: sqlite3.Connection) -> None:
    """Levels you asked to be told about.

    Everything the engine raised until now was derived - a delta crossing a
    threshold, an event inside an expiry. These are the opposite: an arbitrary
    line you drew yourself, on a price or on the book's P&L, which nothing in the
    data suggests on its own.

    The level is part of the alert's key rather than just a column, so moving a
    line makes a new condition that can fire again. Editing 24,000 to 24,500 and
    having it stay quiet because "that alert already fired" would be wrong.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS alert_watch (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            -- 'price' (needs a symbol) or 'pnl' (the book as a whole)
            kind TEXT NOT NULL CHECK (kind IN ('price', 'pnl')),
            symbol TEXT,
            direction TEXT NOT NULL CHECK (direction IN ('above', 'below')),
            level REAL NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            -- a price watch without a symbol has nothing to watch
            CHECK (kind = 'pnl' OR symbol IS NOT NULL)
        );

        CREATE INDEX IF NOT EXISTS idx_alert_watch_enabled ON alert_watch(enabled);
    """)


def _basket_alert_levels(conn: sqlite3.Connection) -> None:
    """Alert levels that belong to one structure rather than to the account.

    Three of the five thresholds were account-wide settings applied to every
    structure, which is the wrong shape: a condor's acceptable delta is not a
    calendar's, and "worst case past your limit" means a different number for
    each. Worse, one of them - a profit target - only ever measured the whole
    Fyers account, so there was no way to ask about the profit on one basket.

    `stop_loss` already existed on `basket` for exactly this purpose and nothing
    ever alerted on it. These two join it, and all three are nullable: null means
    no level set, not a level of zero.
    """
    conn.executescript("""
        ALTER TABLE basket ADD COLUMN profit_target REAL;
        ALTER TABLE basket ADD COLUMN delta_limit REAL;
    """)


def _basket_thresholds(conn: sqlite3.Connection) -> None:
    """The last three thresholds move onto the structure too.

    A worst case, the delta a short counts as tested at, and how many days before
    expiry to warn were left as account-wide numbers when the rest moved - and
    then, when the account panel went, as numbers nothing could edit. Both are
    wrong for the same reason: a condor's tested-short delta is not a strangle's,
    and a warning three days before expiry suits a weekly and not a quarterly.

    Nullable, and null means "use the default". A structure recorded before this
    existed keeps behaving exactly as it did, which is the point of an override
    rather than a required field.
    """
    conn.executescript("""
        ALTER TABLE basket ADD COLUMN worst_case_limit REAL;
        ALTER TABLE basket ADD COLUMN short_delta_limit REAL;
        ALTER TABLE basket ADD COLUMN expiry_warn_days REAL;
    """)


def _perp_order_log(conn: sqlite3.Connection) -> None:
    """Every order this program formed, whether or not it was sent.

    Recorded before the send rather than after the reply, and recorded even when
    the checks refused it. The reason is the obvious one: the interesting question
    after a surprise is "what did it try to do", and a log written only on success
    cannot answer it.

    `sent` is the distinction that matters, and `reason` says why not. The comment
    in the SQL below still mentions a dry run, which this desk no longer has - the
    step has shipped, so its text is left as it was rather than rewritten to match
    a later decision.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS perp_order (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at TEXT NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            order_type TEXT NOT NULL,
            quantity REAL NOT NULL,
            price REAL,
            leverage REAL NOT NULL,
            notional REAL NOT NULL,
            -- 0 for a refusal or a dry run, 1 when it actually left
            sent INTEGER NOT NULL,
            -- why not, or what came back
            reason TEXT NOT NULL,
            venue_order_id TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_perp_order_at ON perp_order(at);
    """)


def _vol_snapshot(conn: sqlite3.Connection) -> None:
    """A daily record of what options cost, per underlying.

    The one piece of market data on this desk that cannot be fetched again. A
    price history can be backfilled from any source years later; what the market
    was charging for a NIFTY straddle on a Tuesday afternoon is gone the moment
    the session ends. Nobody publishes it, the broker does not serve it, and the
    chain endpoint only ever answers "now".

    Which is why this sits in SQLite beside the trades rather than in the bar
    store: `scripts/backup_db.py` copies this file and leaves the DuckDB one
    alone, on the grounds that bars can always be refetched. These cannot.

    One row per underlying per day, keyed so a second pass in the same session
    replaces rather than duplicates - the writer runs on a loop and the last
    reading of the day is the one that closed.

    Call and put implied are stored separately although this broker reports one
    figure per strike and gives both legs the same number - checked across
    eleven strikes, identical every time. They are kept apart because the
    columns cost nothing and a broker that quotes them separately would
    otherwise need a migration; nothing should compute a call-minus-put skew
    from them while this is the source, because it can only ever be zero. The
    skew that is real here runs across strikes rather than between the legs of
    one: on the day this was written NIFTY implied ran 11.11 a percent below
    the money against 10.04 a percent above it.

    `atm_iv` is the average of the call and the put at the money, which is the
    number an option seller means by "implied volatility" and is not the same as
    India VIX: the VIX is a thirty-day constant-maturity figure built across
    strikes, and on the day this was written it read 12.16 against an ATM IV of
    9.85. Both are stored because they answer different questions.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS vol_snapshot (
            underlying TEXT NOT NULL,
            -- The trading day, as YYYY-MM-DD in IST.
            day TEXT NOT NULL,
            -- When within that day the reading was taken.
            at TEXT NOT NULL,
            spot REAL NOT NULL,
            expiry TEXT NOT NULL,
            days_to_expiry REAL NOT NULL,
            atm_strike REAL NOT NULL,
            -- The two legs, and their average. Kept apart because a skew shows
            -- up as a gap between them and the average hides it.
            call_iv REAL,
            put_iv REAL,
            atm_iv REAL,
            -- The at-the-money straddle, which is the market's own expected
            -- move to expiry in points.
            straddle REAL,
            india_vix REAL,
            PRIMARY KEY (underlying, day)
        );

        CREATE INDEX IF NOT EXISTS idx_vol_snapshot_day ON vol_snapshot(day);
    """)


def _broker_fills(conn: sqlite3.Connection) -> None:
    """Every broker fill the desk has seen, and what it did with each.

    Structures were edited only by hand, so a leg closed at the broker stayed
    open on the desk until someone noticed. Reconciling from fills fixes that,
    and needs a memory of which fills were already applied - a sync run twice
    must not close a leg twice.

    `status` is what the fill meant: `applied` (it opened or closed a leg),
    `covered` (a leg already recorded it), `pending` (it opened something no
    structure holds yet - waiting for you to say where it belongs), `outside`
    (a round trip that never touched a structure), `ignored` (you said so), or
    `detached` (its structure or leg was since deleted).

    No foreign keys: a structure deleted as a bookkeeping correction should not
    be blocked by, or take with it, the record of what the broker executed.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS broker_fill (
            fill_id TEXT PRIMARY KEY,
            order_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            price REAL NOT NULL,
            at TEXT NOT NULL,
            status TEXT NOT NULL,
            basket_id INTEGER,
            leg_id INTEGER,
            -- 'open' or 'close', for an applied fill
            action TEXT,
            seen_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_broker_fill_status ON broker_fill(status);
        CREATE INDEX IF NOT EXISTS idx_broker_fill_basket ON broker_fill(basket_id);
    """)


def _preopen(conn: sqlite3.Connection) -> None:
    """NSE's pre-open auction, one row per stock per day.

    NSE shows the latest session and nothing older, so a day not written down
    that day is gone - the same reason `vol_snapshot` exists. Kept for testing
    ideas that read the open before it happens: gap direction, auction volume,
    buy and sell imbalance.

    `preopen_day` says where a day came from: `nse` (the API, with the book and
    buy and sell totals) or `csv` (a file downloaded from the page, which has
    neither). `fingerprint` is what the rows hash to, so the same file saved
    under two names is caught rather than recorded as two days.

    `preopen_book` is the top ten levels the auction was struck from. From the
    API only; the downloaded file does not have it.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS preopen_day (
            day TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            -- NSE's own timestamp for the figures; null from a CSV
            as_of TEXT,
            fingerprint TEXT NOT NULL,
            rows INTEGER NOT NULL,
            recorded_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS preopen_quote (
            day TEXT NOT NULL,
            symbol TEXT NOT NULL,
            series TEXT NOT NULL,
            prev_close REAL NOT NULL,
            final_price REAL NOT NULL,
            final_quantity INTEGER NOT NULL,
            change REAL NOT NULL,
            pct_change REAL NOT NULL,
            iep REAL,
            turnover_cr REAL,
            ffm_cap_cr REAL,
            best_bid REAL,
            best_bid_qty INTEGER,
            best_ask REAL,
            best_ask_qty INTEGER,
            total_buy_qty INTEGER,
            total_sell_qty INTEGER,
            ato_buy_qty INTEGER,
            ato_sell_qty INTEGER,
            imbalance_at_iep INTEGER,
            imbalance_at_market INTEGER,
            year_high REAL,
            year_low REAL,
            PRIMARY KEY (day, symbol)
        );

        CREATE INDEX IF NOT EXISTS idx_preopen_quote_symbol ON preopen_quote(symbol, day);

        CREATE TABLE IF NOT EXISTS preopen_book (
            day TEXT NOT NULL,
            symbol TEXT NOT NULL,
            price REAL NOT NULL,
            buy_qty INTEGER NOT NULL,
            sell_qty INTEGER NOT NULL,
            is_iep INTEGER NOT NULL,
            PRIMARY KEY (day, symbol, price)
        );
    """)


def _preopen_index(conn: sqlite3.Connection) -> None:
    """NIFTY 50's own pre-open figure, beside the day's stocks.

    Where the index was set to open is the gap the options open into, and it is
    the one pre-open number an index options backtest reads first. NSE sends it
    only with the NIFTY 50 list, and only from the API - a downloaded file does
    not have it, so these stay null for a day that came from one.
    """
    for column in ("index_price", "index_change", "index_pct_change"):
        conn.execute(f"ALTER TABLE preopen_day ADD COLUMN {column} REAL")


def _sim_sessions(conn: sqlite3.Connection) -> None:
    """Saved simulator sessions: a moment in the option history and the legs
    traded by hand around it.

    The legs are kept as the page's own JSON rather than as rows: a session is
    reopened whole and never queried by leg, and its shape belongs to the page.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sim_session (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            underlying TEXT NOT NULL,
            -- The moment on screen when saved, ISO.
            at TEXT NOT NULL,
            state TEXT NOT NULL,
            saved_at TEXT NOT NULL
        );
    """)


#: Ordered, append-only. Never edit a step that has shipped.
MIGRATIONS: tuple[Migration, ...] = (
    Migration(version=1, reason="baseline: the schema init_schema creates", apply=_noop),
    Migration(version=2, reason="alert log, active keys and limits move server-side",
              apply=_alert_state),
    Migration(version=3, reason="price and P&L levels you ask to be told about",
              apply=_alert_watches),
    Migration(version=4, reason="per-structure profit target and delta limit",
              apply=_basket_alert_levels),
    Migration(version=5, reason="per-structure worst case, short delta and expiry warning",
              apply=_basket_thresholds),
    Migration(version=6, reason="a log of every perpetual order formed, sent or not",
              apply=_perp_order_log),
    Migration(version=7, reason="what options cost each day, which cannot be fetched later",
              apply=_vol_snapshot),
    Migration(version=8, reason="broker fills, so structures follow what was executed",
              apply=_broker_fills),
    Migration(version=9, reason="NSE's pre-open auction, which is only served on the day",
              apply=_preopen),
    Migration(version=10, reason="NIFTY 50's own pre-open figure, the gap options open into",
              apply=_preopen_index),
    Migration(version=11, reason="simulator sessions saved to come back to",
              apply=_sim_sessions),
)


def schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row else 0


def latest_version() -> int:
    return max((m.version for m in MIGRATIONS), default=0)


def pending(conn: sqlite3.Connection) -> list[Migration]:
    """Steps this database has not run yet, in order."""
    at = schema_version(conn)
    return sorted((m for m in MIGRATIONS if m.version > at), key=lambda m: m.version)


def migrate(conn: sqlite3.Connection) -> list[Migration]:
    """Bring the database up to date, returning what ran.

    Each step and the version bump commit together. A step that raises leaves
    the version where it was, so the next run retries that step rather than
    skipping it and reporting a version the file does not actually have.

    `user_version` takes no parameters - it is a pragma, not a statement - so
    the number is formatted in. It comes from our own migration list, never
    from a caller.
    """
    ran: list[Migration] = []
    for step in pending(conn):
        try:
            step.apply(conn)
            conn.execute(f"PRAGMA user_version = {int(step.version)}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        ran.append(step)
    return ran
