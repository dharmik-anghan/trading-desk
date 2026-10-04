"""Templates and saved strategies over HTTP."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from optbt.spec import VERSION
from optbt.spec import from_dict as spec_from_dict


def a_spec(**overrides: Any) -> dict[str, Any]:
    return {
        "legs": [
            {"side": "sell", "kind": "CE", "strike": {"mode": "atm", "offset": 2}},
            {"side": "sell", "kind": "PE", "strike": {"mode": "atm", "offset": 2}},
        ],
        **overrides,
    }


def test_every_template_is_a_whole_valid_spec(client: TestClient) -> None:
    body = client.get("/api/strategies/templates").json()
    assert [t["id"] for t in body][:2] == ["short_straddle", "short_strangle"]
    for template in body:
        spec = template["spec"]
        # Filled out: every field the builder sets is present.
        assert {"legs", "expiry", "entry", "exit", "hold", "days", "adjust"} <= set(spec)
        spec_from_dict(spec)


def test_the_45_dte_condor_keeps_its_exits(client: TestClient) -> None:
    condor = next(
        t for t in client.get("/api/strategies/templates").json() if t["id"] == "condor_45dte"
    )["spec"]
    assert condor["hold"] == "expiry"
    assert condor["target_credit"] == 0.5
    assert condor["adjust"]["enabled"] is True
    assert {leg["strike"]["mode"] for leg in condor["legs"]} == {"delta"}


def test_save_list_overwrite_delete(client: TestClient) -> None:
    saved = client.post(
        "/api/strategies", json={"name": " BTC strangle ", "underlying": "btc", "spec": a_spec()}
    ).json()
    assert saved["name"] == "BTC strangle"
    assert saved["underlying"] == "BTC"
    assert saved["spec"]["version"] == VERSION
    assert saved["spec"]["expiry"]["series"] == "weekly"

    client.post(
        "/api/strategies", json={"name": "NIFTY straddle", "underlying": "NIFTY", "spec": a_spec()}
    )
    assert [s["name"] for s in client.get("/api/strategies").json()] == [
        "NIFTY straddle",
        "BTC strangle",
    ]
    assert [s["name"] for s in client.get("/api/strategies?underlying=btc").json()] == [
        "BTC strangle"
    ]

    again = client.post(
        "/api/strategies",
        json={
            "id": saved["id"],
            "name": "BTC daily strangle",
            "underlying": "BTC",
            "spec": a_spec(expiry={"series": "daily", "nth": 1}),
        },
    ).json()
    assert again["id"] == saved["id"]
    assert again["spec"]["expiry"]["series"] == "daily"
    assert again["created_at"] == saved["created_at"]

    assert client.delete(f"/api/strategies/{saved['id']}").status_code == 200
    assert client.delete(f"/api/strategies/{saved['id']}").status_code == 404
    assert [s["name"] for s in client.get("/api/strategies").json()] == ["NIFTY straddle"]


def test_overwriting_a_missing_strategy_is_404(client: TestClient) -> None:
    r = client.post(
        "/api/strategies", json={"id": 999, "name": "x", "underlying": "BTC", "spec": a_spec()}
    )
    assert r.status_code == 404


def test_a_spec_the_runner_would_misread_is_refused(client: TestClient) -> None:
    for bad in (
        a_spec(legs=[]),
        a_spec(expiry={"series": "fortnightly"}),
        a_spec(unknown_field=1),
    ):
        r = client.post("/api/strategies", json={"name": "x", "underlying": "BTC", "spec": bad})
        assert r.status_code == 422, bad
    r = client.post("/api/strategies", json={"name": "  ", "underlying": "BTC", "spec": a_spec()})
    assert r.status_code == 422
    r = client.post("/api/strategies", json={"name": "x", "underlying": "B-TC", "spec": a_spec()})
    assert r.status_code == 422
