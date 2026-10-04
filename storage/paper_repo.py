"""Paper sessions and their legs. See `storage/migrations.py` `_paper`."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Literal

_SESSION = (
    "id, name, venue, underlying, created_at, rule_stop, rule_target, "
    "squared_at, squared_reason, squared_net, mode"
)
_LEG = (
    "id, session_id, symbol, side, kind, strike, expiry, delivery, qty, entry_at, "
    "entry_price, entry_fee, entry_index, stop, target, exit_at, exit_price, exit_fee, "
    "exit_reason, enabled, entry_order, exit_order"
)


@dataclass(frozen=True)
class PaperSession:
    id: int
    name: str
    venue: str
    underlying: str
    created_at: datetime
    rule_stop: float | None
    rule_target: float | None
    squared_at: datetime | None
    squared_reason: str | None
    squared_net: float | None
    #: "paper": filled from the market's prices here. "live": by real orders.
    mode: Literal["paper", "live"] = "paper"

    @property
    def live(self) -> bool:
        return self.mode == "live"


@dataclass(frozen=True)
class PaperLeg:
    id: int
    session_id: int
    symbol: str
    side: Literal["buy", "sell"]
    kind: Literal["CE", "PE"]
    strike: float
    expiry: date
    delivery: datetime
    qty: float
    entry_at: datetime
    entry_price: float
    entry_fee: float
    entry_index: float | None
    stop: float | None
    target: float | None
    exit_at: datetime | None
    exit_price: float | None
    exit_fee: float | None
    exit_reason: str | None
    enabled: bool
    #: The venue's ids for the orders that opened and closed it; live legs only.
    entry_order: str | None = None
    exit_order: str | None = None

    @property
    def open(self) -> bool:
        return self.exit_at is None

    @property
    def sign(self) -> int:
        return 1 if self.side == "buy" else -1


def _ts(raw: str | None) -> datetime | None:
    return datetime.fromisoformat(raw) if raw else None


def _session(r: tuple[Any, ...]) -> PaperSession:
    return PaperSession(
        id=r[0],
        name=r[1],
        venue=r[2],
        underlying=r[3],
        created_at=datetime.fromisoformat(r[4]),
        rule_stop=r[5],
        rule_target=r[6],
        squared_at=_ts(r[7]),
        squared_reason=r[8],
        squared_net=r[9],
        mode=r[10],
    )


def _leg(r: tuple[Any, ...]) -> PaperLeg:
    return PaperLeg(
        id=r[0],
        session_id=r[1],
        symbol=r[2],
        side=r[3],
        kind=r[4],
        strike=r[5],
        expiry=date.fromisoformat(r[6]),
        delivery=datetime.fromisoformat(r[7]),
        qty=r[8],
        entry_at=datetime.fromisoformat(r[9]),
        entry_price=r[10],
        entry_fee=r[11],
        entry_index=r[12],
        stop=r[13],
        target=r[14],
        exit_at=_ts(r[15]),
        exit_price=r[16],
        exit_fee=r[17],
        exit_reason=r[18],
        enabled=bool(r[19]),
        entry_order=r[20],
        exit_order=r[21],
    )


# ---------------------------------------------------------------- sessions


def create_session(
    conn: sqlite3.Connection,
    name: str,
    venue: str,
    underlying: str,
    mode: Literal["paper", "live"] = "paper",
) -> PaperSession:
    cur = conn.execute(
        "INSERT INTO paper_session (name, venue, underlying, created_at, mode) VALUES (?,?,?,?,?)",
        (name, venue, underlying, datetime.now(UTC).isoformat(), mode),
    )
    conn.commit()
    found = get_session(conn, int(cur.lastrowid or 0))
    assert found is not None
    return found


def get_session(conn: sqlite3.Connection, session_id: int) -> PaperSession | None:
    r = conn.execute(f"SELECT {_SESSION} FROM paper_session WHERE id = ?", (session_id,)).fetchone()
    return _session(r) if r else None


def sessions(conn: sqlite3.Connection, underlying: str | None = None) -> list[PaperSession]:
    """Newest first - one underlying's, or all."""
    if underlying is None:
        rows = conn.execute(f"SELECT {_SESSION} FROM paper_session ORDER BY id DESC").fetchall()
    else:
        rows = conn.execute(
            f"SELECT {_SESSION} FROM paper_session WHERE underlying = ? ORDER BY id DESC",
            (underlying,),
        ).fetchall()
    return [_session(r) for r in rows]


def rename_session(conn: sqlite3.Connection, session_id: int, name: str) -> None:
    conn.execute("UPDATE paper_session SET name = ? WHERE id = ?", (name, session_id))
    conn.commit()


def set_rule(
    conn: sqlite3.Connection, session_id: int, stop: float | None, target: float | None
) -> None:
    conn.execute(
        "UPDATE paper_session SET rule_stop = ?, rule_target = ? WHERE id = ?",
        (stop, target, session_id),
    )
    conn.commit()


def squared(
    conn: sqlite3.Connection, session_id: int, at: datetime, reason: str, net: float
) -> None:
    """The rule fired: say when and why, and disarm it - left armed, it would
    close whatever is opened next."""
    conn.execute(
        "UPDATE paper_session SET squared_at = ?, squared_reason = ?, squared_net = ?, "
        "rule_stop = NULL, rule_target = NULL WHERE id = ?",
        (at.isoformat(), reason, net, session_id),
    )
    conn.commit()


def delete_session(conn: sqlite3.Connection, session_id: int) -> bool:
    conn.execute("DELETE FROM paper_leg WHERE session_id = ?", (session_id,))
    cur = conn.execute("DELETE FROM paper_session WHERE id = ?", (session_id,))
    conn.commit()
    return cur.rowcount > 0


# -------------------------------------------------------------------- legs


def add_leg(
    conn: sqlite3.Connection,
    session_id: int,
    *,
    symbol: str,
    side: str,
    kind: str,
    strike: float,
    expiry: date,
    delivery: datetime,
    qty: float,
    entry_at: datetime,
    entry_price: float,
    entry_fee: float,
    entry_index: float | None,
    entry_order: str | None = None,
) -> PaperLeg:
    cur = conn.execute(
        "INSERT INTO paper_leg (session_id, symbol, side, kind, strike, expiry, delivery, qty, "
        "entry_at, entry_price, entry_fee, entry_index, entry_order) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            session_id,
            symbol,
            side,
            kind,
            strike,
            expiry.isoformat(),
            delivery.isoformat(),
            qty,
            entry_at.isoformat(),
            entry_price,
            entry_fee,
            entry_index,
            entry_order,
        ),
    )
    conn.commit()
    found = get_leg(conn, int(cur.lastrowid or 0))
    assert found is not None
    return found


def get_leg(conn: sqlite3.Connection, leg_id: int) -> PaperLeg | None:
    r = conn.execute(f"SELECT {_LEG} FROM paper_leg WHERE id = ?", (leg_id,)).fetchone()
    return _leg(r) if r else None


def legs(conn: sqlite3.Connection, session_id: int) -> list[PaperLeg]:
    rows = conn.execute(
        f"SELECT {_LEG} FROM paper_leg WHERE session_id = ? ORDER BY id", (session_id,)
    ).fetchall()
    return [_leg(r) for r in rows]


def live_legs_since(conn: sqlite3.Connection, since: datetime) -> list[PaperLeg]:
    """Every leg of every live session entered at or after `since`."""
    rows = conn.execute(
        f"SELECT {', '.join('l.' + c.strip() for c in _LEG.split(','))} FROM paper_leg l "
        "JOIN paper_session s ON s.id = l.session_id "
        "WHERE s.mode = 'live' AND l.entry_at >= ? ORDER BY l.id",
        (since.isoformat(),),
    ).fetchall()
    return [_leg(r) for r in rows]


def open_legs(conn: sqlite3.Connection) -> list[PaperLeg]:
    """Every open leg in every session - what the watcher looks after."""
    rows = conn.execute(
        f"SELECT {_LEG} FROM paper_leg WHERE exit_at IS NULL ORDER BY id"
    ).fetchall()
    return [_leg(r) for r in rows]


def set_levels(
    conn: sqlite3.Connection,
    leg_id: int,
    *,
    stop: float | None,
    target: float | None,
    enabled: bool,
) -> None:
    conn.execute(
        "UPDATE paper_leg SET stop = ?, target = ?, enabled = ? WHERE id = ?",
        (stop, target, int(enabled), leg_id),
    )
    conn.commit()


def close_leg(
    conn: sqlite3.Connection,
    leg_id: int,
    *,
    at: datetime,
    price: float,
    fee: float,
    reason: str,
    order: str | None = None,
) -> bool:
    """Close an open leg. False if it was already closed - two closers racing,
    the watcher and a click, must not both book an exit."""
    cur = conn.execute(
        "UPDATE paper_leg SET exit_at = ?, exit_price = ?, exit_fee = ?, exit_reason = ?, "
        "exit_order = ? WHERE id = ? AND exit_at IS NULL",
        (at.isoformat(), price, fee, reason, order, leg_id),
    )
    conn.commit()
    return cur.rowcount > 0


def set_entry_order(conn: sqlite3.Connection, leg_id: int, order: str) -> None:
    conn.execute("UPDATE paper_leg SET entry_order = ? WHERE id = ?", (order, leg_id))
    conn.commit()


def delete_leg(conn: sqlite3.Connection, leg_id: int) -> bool:
    """Remove a closed leg from its session. An open one has to be exited first."""
    cur = conn.execute("DELETE FROM paper_leg WHERE id = ? AND exit_at IS NOT NULL", (leg_id,))
    conn.commit()
    return cur.rowcount > 0
