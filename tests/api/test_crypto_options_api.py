"""The crypto options routes, over the captured Shark responses."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from api.app import app
from api.deps import get_crypto_options
from tests.broker.test_shark_options import broker, fed


@pytest.fixture
def crypto(client: TestClient) -> Iterator[TestClient]:
    feed = fed()
    app.dependency_overrides[get_crypto_options] = lambda: broker(feed)
    yield client


def test_lists_the_underlyings_with_their_terms(crypto: TestClient) -> None:
    body = crypto.get("/api/crypto-options/underlyings").json()
    assert [u["underlying"] for u in body] == ["BTC", "ETH"]
    btc = body[0]
    assert btc["spot"] > 0
    assert btc["fee_cap_pct"] == 7


def test_lists_expiries(crypto: TestClient) -> None:
    body = crypto.get("/api/crypto-options/expiries", params={"underlying": "BTC"}).json()
    assert body[0] == {"date": "05-10-2026", "token": "1791187200000", "weekly": True}


def test_serves_a_chain(crypto: TestClient) -> None:
    body = crypto.get(
        "/api/crypto-options/chain", params={"underlying": "BTC", "strikes": 2}
    ).json()
    assert len(body["rows"]) == 10
    assert all(r["mark"] > 0 for r in body["rows"])
    assert body["underlying_ltp"] > 0


def test_serves_a_book(crypto: TestClient) -> None:
    body = crypto.get(
        "/api/crypto-options/book", params={"symbol": "BTC-5OCT26-85500-P-USDT"}
    ).json()
    assert body["bids"][0]["price"] < body["asks"][0]["price"]


def test_the_desk_switcher_is_not_offered_it_as_a_desk(client: TestClient) -> None:
    venue = next(v for v in client.get("/api/venues").json() if v["id"] == "shark_options")
    assert venue["asset_class"] == "crypto_options"
    assert "trading" not in venue["capabilities"]
