"""Saved simulator sessions. See `storage/migrations.py` `_sim_sessions`."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True)
class SimSession:
    id: int
    name: str
    underlying: str
    at: datetime
    state: dict[str, Any]
    saved_at: datetime


def _row(r: tuple[Any, ...]) -> SimSession:
    return SimSession(
        id=r[0],
        name=r[1],
        underlying=r[2],
        at=datetime.fromisoformat(r[3]),
        state=json.loads(r[4]),
        saved_at=datetime.fromisoformat(r[5]),
    )


def save(
    conn: sqlite3.Connection,
    name: str,
    underlying: str,
    at: datetime,
    state: dict[str, Any],
    session_id: int | None = None,
) -> SimSession:
    """A new session, or - with `session_id` - that one overwritten."""
    now = datetime.now(UTC).isoformat()
    if session_id is None:
        cur = conn.execute(
            "INSERT INTO sim_session (name, underlying, at, state, saved_at) VALUES (?,?,?,?,?)",
            (name, underlying, at.isoformat(), json.dumps(state), now),
        )
        session_id = int(cur.lastrowid or 0)
    else:
        cur = conn.execute(
            "UPDATE sim_session SET name = ?, underlying = ?, at = ?, state = ?, saved_at = ? "
            "WHERE id = ?",
            (name, underlying, at.isoformat(), json.dumps(state), now, session_id),
        )
        if cur.rowcount == 0:
            raise KeyError(session_id)
    conn.commit()
    found = get(conn, session_id)
    assert found is not None
    return found


def get(conn: sqlite3.Connection, session_id: int) -> SimSession | None:
    r = conn.execute(
        "SELECT id, name, underlying, at, state, saved_at FROM sim_session WHERE id = ?",
        (session_id,),
    ).fetchone()
    return _row(r) if r else None


def listing(conn: sqlite3.Connection) -> list[SimSession]:
    """Every saved session, most recently saved first."""
    rows = conn.execute(
        "SELECT id, name, underlying, at, state, saved_at FROM sim_session "
        "ORDER BY saved_at DESC, id DESC"
    ).fetchall()
    return [_row(r) for r in rows]


def delete(conn: sqlite3.Connection, session_id: int) -> bool:
    cur = conn.execute("DELETE FROM sim_session WHERE id = ?", (session_id,))
    conn.commit()
    return cur.rowcount > 0
