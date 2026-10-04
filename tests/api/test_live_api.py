"""The live strategy builder's routes, over a fake NSE and the captured Shark chain."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

import api.routers.live as live
from api.app import app
from api.deps import get_paper_markets
from broker.fyers import SYMBOLS
from optbt.templates import TEMPLATES
from paper.markets import NseMarket, SharkMarket
from tests.broker.test_shark_options import broker, fed
from tests.paper.fakes import LOT, FakeFyers, symbol
from tests.paper.test_markets import OPEN

BTC_PUT = "BTC-5OCT26-85500-P-USDT"


@pytest.fixture
def client_(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(live, "_now", lambda: OPEN)
    fake = FakeFyers()
    markets = {
        "fyers": NseMarket(fake, SYMBOLS, fake, clock=lambda: OPEN),  # type: ignore[arg-type]
        "shark_options": SharkMarket(broker(fed())),
    }
    app.dependency_overrides[get_paper_markets] = lambda: markets
    yield client


def test_markets_list_the_nse_first_then_crypto(client_: TestClient) -> None:
    body = client_.get("/api/live/markets").json()
    names = [(m["group"], m["underlying"]) for m in body]
    assert names[0] == ("NSE", "NIFTY")
    assert ("Crypto", "BTC") in names and ("Crypto", "ETH") in names


def test_state_is_the_same_shape_on_both_markets(client_: TestClient) -> None:
    nifty = client_.get("/api/live/state", params={"underlying": "NIFTY"}).json()
    btc = client_.get("/api/live/state", params={"underlying": "BTC", "strikes": 2}).json()
    assert (nifty["currency"], nifty["step"], nifty["open"]) == ("INR", LOT, True)
    assert (btc["currency"], btc["step"]) == ("USDT", 0.01)
    for body in (nifty, btc):
        side = body["rows"][0]["ce"]
        assert side["symbol"] and side["bid"] is not None
    assert client_.get("/api/live/state", params={"underlying": "DOGE"}).status_code == 404


def test_a_template_resolves_to_contracts_and_carries_its_exits(client_: TestClient) -> None:
    spec = next(t.spec for t in TEMPLATES if t.id == "short_strangle")
    body = client_.post("/api/live/resolve", json={"underlying": "NIFTY", "spec": spec}).json()
    assert [(d["kind"], d["strike"], d["qty"]) for d in body["legs"]] == [
        ("CE", 25100, LOT),
        ("PE", 24900, LOT),
    ]
    assert body["legs"][0]["stop"] == {"kind": "pct", "value": 0.25}


def test_a_draft_is_previewed_before_it_is_traded(client_: TestClient) -> None:
    legs = [
        {"symbol": symbol(25100, "CE"), "side": "sell", "qty": LOT},
        {"symbol": symbol(24900, "PE"), "side": "sell", "qty": LOT},
    ]
    body = client_.post("/api/live/preview", json={"underlying": "NIFTY", "legs": legs}).json()
    assert body["premium"] > 0 and body["fees"] > 0
    assert body["margin"] > 0
    assert body["payoff"]["max_profit"] > 0
    assert body["problems"] == {}


def test_a_basket_opens_a_session_with_its_stops_and_rule(client_: TestClient) -> None:
    legs = [
        {
            "symbol": symbol(25100, "CE"),
            "side": "sell",
            "qty": LOT,
            "stop": {"kind": "pct", "value": 0.25},
        },
        {"symbol": symbol(24900, "PE"), "side": "sell", "qty": LOT},
    ]
    body = client_.post(
        "/api/live/orders",
        json={"underlying": "NIFTY", "legs": legs, "rule": {"target_credit": 0.5}},
    ).json()
    session = body["session"]
    assert session["venue"] == "fyers" and session["underlying"] == "NIFTY"
    call, put = body["legs"]
    assert call["stop"] == pytest.approx(call["entry_price"] * 1.25)
    assert put["stop"] is None
    credit = (call["entry_price"] + put["entry_price"]) * LOT
    assert session["rule_target"] == pytest.approx(credit * 0.5)
    state = client_.get(
        "/api/live/state", params={"underlying": "NIFTY", "session_id": session["id"]}
    ).json()
    assert [leg["id"] for leg in state["legs"]] == [call["id"], put["id"]]
    assert state["margin"] > 0


def test_a_basket_the_market_refuses_leaves_nothing_behind(client_: TestClient) -> None:
    legs = [
        {"symbol": BTC_PUT, "side": "sell", "qty": 0.05},
        {"symbol": BTC_PUT, "side": "buy", "qty": 400},
    ]
    r = client_.post("/api/live/orders", json={"underlying": "BTC", "legs": legs})
    assert r.status_code == 409
    assert client_.get("/api/live/sessions").json() == []


def test_a_session_keeps_to_its_underlying(client_: TestClient) -> None:
    body = client_.post(
        "/api/live/orders",
        json={"underlying": "BTC", "legs": [{"symbol": BTC_PUT, "side": "sell", "qty": 0.05}]},
    ).json()
    r = client_.post(
        "/api/live/orders",
        json={
            "underlying": "NIFTY",
            "session_id": body["session"]["id"],
            "legs": [{"symbol": symbol(25000, "CE"), "side": "buy", "qty": LOT}],
        },
    )
    assert r.status_code == 409


def test_exit_patch_and_remove(client_: TestClient) -> None:
    body = client_.post(
        "/api/live/orders",
        json={"underlying": "BTC", "legs": [{"symbol": BTC_PUT, "side": "sell", "qty": 0.05}]},
    ).json()
    leg = body["legs"][0]
    url = f"/api/live/legs/{leg['id']}"
    assert client_.patch(url, json={"stop": 1}).status_code == 422
    assert client_.patch(url, json={"stop": 600}).json()["stop"] == 600
    assert client_.delete(url).status_code == 409
    closed = client_.post(f"{url}/exit").json()
    assert closed["status"] == "closed" and closed["exit_reason"] == "exit"
    assert client_.delete(url).status_code == 200


class FakeExecutor:
    venue = "shark_options"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, float]] = []

    def execute(self, symbol: str, side: str, qty: float, now: object) -> object:
        from paper.markets import Execution

        self.sent.append((symbol, side, qty))
        return Execution(price=421.0, qty=qty, fee=0.5, order_id=f"real{len(self.sent)}")


class FakeAccount:
    def __init__(self) -> None:
        self.held: list[object] = []

    def positions(self) -> list[object]:
        return self.held

    def wallet(self) -> object:
        from broker.shark.options_account import OptionsWallet

        return OptionsWallet("USDT", 500.0, 500.0, 0.0, 0.0, 0.0)


@pytest.fixture
def real(client_: TestClient) -> Iterator[tuple[TestClient, FakeExecutor, FakeAccount]]:
    from api.deps import get_executors, get_live_limits, get_shark_options_account
    from paper.live import Limits

    executor, account = FakeExecutor(), FakeAccount()
    app.dependency_overrides[get_executors] = lambda: {"shark_options": executor}
    app.dependency_overrides[get_shark_options_account] = lambda: account
    app.dependency_overrides[get_live_limits] = lambda: Limits(True, 2000.0, 50.0)
    yield client_, executor, account


LIVE = {
    "underlying": "BTC",
    "mode": "live",
    "legs": [{"symbol": BTC_PUT, "side": "sell", "qty": 0.01}],
}


def test_live_is_offered_only_where_an_executor_is(real: tuple) -> None:  # type: ignore[type-arg]
    client, _, _ = real
    flags = {m["underlying"]: m["live"] for m in client.get("/api/live/markets").json()}
    assert flags["BTC"] and flags["ETH"] and not flags["NIFTY"]


def test_a_live_basket_needs_confirming_and_live_trading_on(client_: TestClient) -> None:
    # No executor: live trading is off.
    r = client_.post("/api/live/orders", json={**LIVE, "confirm": True})
    assert r.status_code == 409 and "not available" in r.json()["detail"]


def test_a_live_basket_without_confirm_is_refused(real: tuple) -> None:  # type: ignore[type-arg]
    client, executor, _ = real
    r = client.post("/api/live/orders", json=LIVE)
    assert r.status_code == 409 and "confirm" in r.json()["detail"]
    assert executor.sent == []


def test_a_live_basket_is_sent_and_booked_at_the_venues_price(real: tuple) -> None:  # type: ignore[type-arg]
    client, executor, account = real
    body = client.post("/api/live/orders", json={**LIVE, "confirm": True}).json()
    assert body["session"]["mode"] == "live"
    assert body["session"]["name"].startswith("LIVE BTC")
    assert body["legs"][0]["entry_price"] == 421.0
    assert executor.sent == [(BTC_PUT, "sell", 0.01)]
    sid = body["session"]["id"]

    # The venue holds nothing yet: the state says they disagree.
    state = client.get("/api/live/state", params={"underlying": "BTC", "session_id": sid}).json()
    assert state["account"]["available"] == 500.0
    assert state["account"]["mismatches"] == [f"{BTC_PUT}: Shark holds +0, this session -0.01"]

    from broker.shark.options_account import OptionsPosition

    account.held = [OptionsPosition(BTC_PUT, "sell", 0.01, 421.0, 420.0, 0.01)]
    state = client.get("/api/live/state", params={"underlying": "BTC", "session_id": sid}).json()
    assert state["account"]["mismatches"] == []

    # A paper order cannot join a live session, nor a live one a paper session.
    r = client.post("/api/live/orders", json={**LIVE, "mode": "paper", "session_id": sid})
    assert r.status_code == 409


def test_closing_a_live_leg_needs_confirming(real: tuple) -> None:  # type: ignore[type-arg]
    client, executor, _ = real
    leg = client.post("/api/live/orders", json={**LIVE, "confirm": True}).json()["legs"][0]
    url = f"/api/live/legs/{leg['id']}/exit"
    assert client.post(url).status_code == 409
    closed = client.post(url, params={"confirm": True}).json()
    assert closed["status"] == "closed" and closed["exit_price"] == 421.0
    assert executor.sent[-1] == (BTC_PUT, "buy", 0.01)


def test_a_leg_over_the_notional_cap_never_reaches_the_venue(real: tuple) -> None:  # type: ignore[type-arg]
    client, executor, _ = real
    big = {**LIVE, "confirm": True, "legs": [{"symbol": BTC_PUT, "side": "sell", "qty": 0.05}]}
    r = client.post("/api/live/orders", json=big)
    assert r.status_code == 409 and "cap" in r.json()["detail"]
    assert executor.sent == []
