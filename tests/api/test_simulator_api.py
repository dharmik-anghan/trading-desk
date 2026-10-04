"""The simulator API: a moment, legs sent and returned, sessions saved."""

from __future__ import annotations

from datetime import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.deps import get_db_path
from api.routers.simulator import router
from optbt.data.models import Kind
from tests.optbt.test_option_engine import DAY, EXPIRY, LOT, Market


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    m = Market()
    m.index(DAY)
    for strike in (23400.0, 23450.0, 23500.0):
        ce = {time(11, 0): (110.0, 130.0, 110.0, 125.0)} if strike == 23450 else None
        m.option(DAY, strike, Kind.CALL, 100.0, changes=ce)
        m.option(DAY, strike, Kind.PUT, 100.0)
    path = tmp_path / "options.duckdb"
    m.conn.execute(f"ATTACH '{path}' AS out")
    m.conn.execute("COPY FROM DATABASE memory TO out")
    m.conn.execute("DETACH out")
    monkeypatch.setenv("OPTBT_STORE", str(path))
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db_path] = lambda: tmp_path / "trading.db"
    return TestClient(app)


def test_a_leg_sent_unpriced_comes_back_filled_and_stops_on_a_step(client: TestClient) -> None:
    leg = {
        "id": "a", "side": "sell", "kind": "CE", "strike": 23450, "expiry": str(EXPIRY),
        "lots": 1, "entry_at": f"{DAY}T10:30:00",
    }
    first = client.post("/api/sim/moment", json={"at": f"{DAY}T10:30:00", "legs": [leg]})
    assert first.status_code == 200, first.text
    body = first.json()
    (got,) = body["legs"]
    assert (got["entry_price"], got["status"], got["lot_size"]) == (100.0, "open", LOT)
    assert body["atm"] == 23450.0

    keep = {k: got[k] for k in leg} | {"entry_price": got["entry_price"], "stop": 120.0}
    body = client.post(
        "/api/sim/moment",
        json={"at": f"{DAY}T10:30:00", "move": "+1h", "since": f"{DAY}T10:30:00", "legs": [keep]},
    ).json()
    assert body["at"] == f"{DAY}T11:30:00"
    (got,) = body["legs"]
    assert (got["status"], got["exit_price"], got["exit_reason"]) == ("closed", 120.0, "stop")
    assert body["payoff"]["realised"] == pytest.approx(-20 * LOT)


def test_a_bad_move_is_refused(client: TestClient) -> None:
    assert client.post("/api/sim/moment", json={"move": "+5x"}).status_code == 422


def test_sessions_save_list_overwrite_and_delete(client: TestClient) -> None:
    saved = client.post(
        "/api/sim/sessions",
        json={"name": "Straddle", "at": f"{DAY}T10:30:00", "state": {"legs": [], "multiplier": 2}},
    ).json()
    again = client.post(
        "/api/sim/sessions",
        json={"id": saved["id"], "name": "Straddle 2", "at": f"{DAY}T11:00:00", "state": {}},
    ).json()
    assert again["id"] == saved["id"]
    listed = client.get("/api/sim/sessions").json()
    assert [(s["name"], s["at"]) for s in listed] == [("Straddle 2", f"{DAY}T11:00:00")]
    assert client.delete(f"/api/sim/sessions/{saved['id']}").json() == {"deleted": True}
    assert client.get("/api/sim/sessions").json() == []
    assert client.delete(f"/api/sim/sessions/{saved['id']}").status_code == 404
