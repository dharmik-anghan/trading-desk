"""The simulator API: a moment, legs sent and returned, sessions saved."""

from __future__ import annotations

from datetime import date, time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.deps import get_db_path
from api.routers import simulator
from api.routers.simulator import router
from optbt.data.live import Progress
from optbt.data.models import Kind
from tests.optbt.test_option_engine import DAY, EXPIRY, LOT, Market


class _Fetcher:
    def reset(self) -> None:
        self.started: list[tuple[object, str, object, object]] = []
        self._asked: set[object] = set()
        self.progress: Progress | None = None

    def asked(self, key: object) -> bool:
        return key in self._asked

    @property
    def running(self) -> Progress | None:
        return self.progress

    def start(self, key: object, underlying: str, day: object, expiry: object) -> Progress:
        self._asked.add(key)
        self.started.append((key, underlying, day, expiry))
        self.progress = Progress(underlying, day, expiry)  # type: ignore[arg-type]
        return self.progress


FETCHER = _Fetcher()


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
    # Never Fyers from a test: a stand-in that records what it was asked for.
    monkeypatch.setattr(simulator, "_live_fetcher", lambda: FETCHER)
    FETCHER.reset()
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


def test_a_held_moment_fetches_nothing(client: TestClient) -> None:
    body = client.post("/api/sim/moment", json={"at": f"{DAY}T10:30:00"}).json()
    assert body["loading"] is None
    assert FETCHER.started == []


def test_a_weekday_the_store_lacks_is_fetched_rather_than_skipped(client: TestClient) -> None:
    """Friday the 18th is not in the store: the page waits for it rather than
    being moved to the Monday it does have."""
    body = client.post("/api/sim/moment", json={"at": "2026-09-18T10:30:00"}).json()
    assert body == {"loading": body["loading"]}
    assert body["loading"]["day"] == "2026-09-18"
    (started,) = FETCHER.started
    assert started[1:] == ("NIFTY", date(2026, 9, 18), None)
    # Asked once: the same request again does not start it again.
    FETCHER.progress = None
    client.post("/api/sim/moment", json={"at": "2026-09-18T10:30:00"})
    assert len(FETCHER.started) == 1


def test_an_index_with_nothing_held_is_fetched(client: TestClient) -> None:
    body = client.post(
        "/api/sim/moment", json={"underlying": "BANKNIFTY", "at": f"{DAY}T10:30:00"}
    ).json()
    assert body["loading"]["underlying"] == "BANKNIFTY"
