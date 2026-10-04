"""Saved strategies. See `storage/migrations.py` `_strategies`."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

_COLUMNS = "id, name, underlying, spec, created_at, saved_at"


@dataclass(frozen=True)
class SavedStrategy:
    id: int
    name: str
    underlying: str
    spec: dict[str, Any]
    created_at: datetime
    saved_at: datetime


def _row(r: tuple[Any, ...]) -> SavedStrategy:
    return SavedStrategy(
        id=r[0],
        name=r[1],
        underlying=r[2],
        spec=json.loads(r[3]),
        created_at=datetime.fromisoformat(r[4]),
        saved_at=datetime.fromisoformat(r[5]),
    )


def save(
    conn: sqlite3.Connection,
    name: str,
    underlying: str,
    spec: dict[str, Any],
    strategy_id: int | None = None,
) -> SavedStrategy:
    """A new strategy, or - with `strategy_id` - that one overwritten."""
    now = datetime.now(UTC).isoformat()
    if strategy_id is None:
        cur = conn.execute(
            "INSERT INTO strategy (name, underlying, spec, created_at, saved_at) "
            "VALUES (?,?,?,?,?)",
            (name, underlying, json.dumps(spec), now, now),
        )
        strategy_id = int(cur.lastrowid or 0)
    else:
        cur = conn.execute(
            "UPDATE strategy SET name = ?, underlying = ?, spec = ?, saved_at = ? WHERE id = ?",
            (name, underlying, json.dumps(spec), now, strategy_id),
        )
        if cur.rowcount == 0:
            raise KeyError(strategy_id)
    conn.commit()
    found = get(conn, strategy_id)
    assert found is not None
    return found


def get(conn: sqlite3.Connection, strategy_id: int) -> SavedStrategy | None:
    r = conn.execute(f"SELECT {_COLUMNS} FROM strategy WHERE id = ?", (strategy_id,)).fetchone()
    return _row(r) if r else None


def listing(conn: sqlite3.Connection, underlying: str | None = None) -> list[SavedStrategy]:
    """Saved strategies, most recently saved first - one underlying's, or all."""
    if underlying is None:
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM strategy ORDER BY saved_at DESC, id DESC"
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM strategy WHERE underlying = ? ORDER BY saved_at DESC, id DESC",
            (underlying,),
        ).fetchall()
    return [_row(r) for r in rows]


def delete(conn: sqlite3.Connection, strategy_id: int) -> bool:
    cur = conn.execute("DELETE FROM strategy WHERE id = ?", (strategy_id,))
    conn.commit()
    return cur.rowcount > 0
