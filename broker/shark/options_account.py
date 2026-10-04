"""A Shark options account: its wallet, positions and orders - and real orders.

Signed with the same key and HMAC scheme as the perpetuals account, against the
options service's host. The shapes are the venue web app's, read from its code
(nothing about options is in the published docs): an order is
`{side, symbol, placeType: "ORDER_FORM", quantity, type, price?}` with the
quantity in the coin, and a cancel names the order's `clientOrderId`.

Kept apart from `SharkOptionsBroker`, which reads the public market and needs no
key, so nothing that only draws a chain can reach the account.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Literal

import requests

from broker.errors import BrokerError, BrokerUnreachable, classify_status
from broker.shark.options import BASE_URL, TIMEOUT
from broker.shark.signing import headers, signed_body, signed_query

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class OptionsWallet:
    margin_asset: str
    total: float
    available: float
    position_im: float
    position_mm: float
    unrealised: float


@dataclass(frozen=True)
class OptionsPosition:
    symbol: str
    side: Literal["buy", "sell"]
    #: In the coin, unsigned; `side` says which way.
    size: float
    entry: float
    mark: float | None
    unrealised: float | None


@dataclass(frozen=True)
class OptionsOrder:
    client_order_id: str
    symbol: str
    side: Literal["buy", "sell"]
    status: str
    qty: float
    filled: float
    avg_price: float | None


def _f(value: Any) -> float | None:
    try:
        return None if value is None or value == "" else float(value)
    except (TypeError, ValueError):
        return None


def _side(value: Any) -> Literal["buy", "sell"]:
    return "buy" if str(value).upper() in ("BUY", "LONG") else "sell"


def _rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        payload = payload.get("data", [])
    return [r for r in payload if isinstance(r, dict)] if isinstance(payload, list) else []


def parse_order(row: dict[str, Any]) -> OptionsOrder:
    return OptionsOrder(
        client_order_id=str(row.get("clientOrderId") or row.get("id") or ""),
        symbol=str(row.get("symbol", "")),
        side=_side(row.get("side")),
        status=str(row.get("orderStatus") or row.get("status") or "").upper(),
        qty=_f(row.get("qty") or row.get("quantity")) or 0.0,
        filled=_f(row.get("cumExecQty") or row.get("executedQty")) or 0.0,
        avg_price=_f(row.get("avgPrice")) or None,
    )


class SharkOptionsAccount:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        base_url: str = BASE_URL,
        session: requests.Session | None = None,
    ) -> None:
        self._key = api_key
        self._secret = api_secret
        self._base = base_url.rstrip("/")
        self._session = session or requests.Session()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self._base}{path}"
        data: str | None = None
        if body is not None:
            data, signature = signed_body(self._secret, body)
            sent = headers(self._key, signature, json_body=True)
        else:
            query, signature = signed_query(self._secret, params)
            url = f"{url}?{query}"
            sent = headers(self._key, signature)
        try:
            response = self._session.request(method, url, data=data, headers=sent, timeout=TIMEOUT)
        except requests.RequestException as exc:
            # Never the URL: a signed GET carries its signature in it.
            raise BrokerUnreachable(f"cannot reach Shark options ({type(exc).__name__})") from exc
        if response.status_code >= 400:
            try:
                err = response.json()
                message = str(err.get("details") or err.get("message") or response.text[:160])
            except ValueError:
                message = response.text[:160]
            raise classify_status(response.status_code, message)(f"Shark options: {message}")
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise BrokerError(
                f"Shark options returned a non-JSON body: {response.text[:120]}"
            ) from exc

    # -------------------------------------------------------------------- reads

    def wallet(self) -> OptionsWallet:
        w = self._request("GET", "/v1/wallet/options-wallet/details")
        if not isinstance(w, dict):
            raise BrokerError("Shark sent no options wallet")
        return OptionsWallet(
            margin_asset=str(w.get("marginAsset") or "INR"),
            total=_f(w.get("totalBalance")) or 0.0,
            available=_f(w.get("availableBalance")) or 0.0,
            position_im=_f(w.get("totalPositionIM")) or 0.0,
            position_mm=_f(w.get("totalPositionMM")) or 0.0,
            unrealised=_f(w.get("totalUpnl")) or 0.0,
        )

    def positions(self) -> list[OptionsPosition]:
        out = []
        for r in _rows(self._request("GET", "/v1/positions/open-positions")):
            size = _f(r.get("positionSize")) or 0.0
            if size == 0:
                continue
            out.append(
                OptionsPosition(
                    symbol=str(r.get("symbol", "")),
                    side=_side(r.get("side")) if r.get("side") else ("buy" if size > 0 else "sell"),
                    size=abs(size),
                    entry=_f(r.get("entryPrice")) or 0.0,
                    mark=_f(r.get("markPrice")),
                    unrealised=_f(r.get("unrealisedPnl")),
                )
            )
        return out

    def open_orders(self) -> list[OptionsOrder]:
        return [parse_order(r) for r in _rows(self._request("GET", "/v1/order/open-orders"))]

    def recent_orders(self) -> list[OptionsOrder]:
        """The latest orders, filled or not, newest first."""
        rows = _rows(self._request("GET", "/v1/order/order-history", params={"pageSize": 25}))
        return [parse_order(r) for r in rows]

    # ------------------------------------------------------------------- orders

    def place_market(self, symbol: str, side: Literal["buy", "sell"], qty: float) -> OptionsOrder:
        """A real market order. Irreversible: callers confirm and check limits first."""
        body = {
            "placeType": "ORDER_FORM",
            "symbol": symbol,
            "side": side.upper(),
            "type": "MARKET",
            "quantity": qty,
        }
        payload = self._request("POST", "/v1/order/place-order", body=body)
        log.info("shark options order %s %s %s: %s", side, qty, symbol, json.dumps(payload)[:300])
        if not isinstance(payload, dict):
            raise BrokerError("Shark accepted the order but said nothing about it")
        inner = payload.get("data")
        found: dict[str, Any] = inner if isinstance(inner, dict) else payload
        order = parse_order(
            {**found, "symbol": found.get("symbol") or symbol, "side": found.get("side") or side}
        )
        if not order.client_order_id:
            raise BrokerError(f"Shark returned no order id: {json.dumps(payload)[:160]}")
        return order

    def cancel(self, client_order_id: str) -> None:
        self._request("DELETE", "/v1/order/delete-order", body={"clientOrderId": client_order_id})
