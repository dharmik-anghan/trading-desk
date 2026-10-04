"""Real orders behind a live session: sending them, reading their fills back,
and the checks that come first.

A live session uses the same desk as a paper one - the same legs, stops,
targets, exit-all rule and watcher - with one difference: where a paper fill is
worked out from the book, a live one is a market order the venue fills, and the
leg records what the venue says it filled at.

The checks are seatbelts against this software being wrong, not opinions about a
trade, which is why they are blunt: live trading switched on in the settings,
each leg under the notional cap, today's live losses under the daily limit, and
the margin the basket needs within what the wallet holds.
"""

from __future__ import annotations

import logging
import sqlite3
import time as clock
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from broker.errors import BrokerError
from broker.shark.options import SharkOptionsBroker
from broker.shark.options_account import OptionsOrder, SharkOptionsAccount
from paper import desk
from paper.markets import Execution, Held, PaperError, PaperMarket
from storage import paper_repo
from venues.calendar import IST

log = logging.getLogger(__name__)

#: How long a market order may take to report filled before it is cancelled.
FILL_WAIT = 6.0

DONE = {"FILLED"}
DEAD = {"CANCELED", "CANCELLED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH"}


class LiveError(PaperError):
    """A real order that did not go as asked. Its message says what is left
    standing, so nobody has to guess whether a position exists."""


@dataclass(frozen=True)
class Limits:
    enabled: bool
    #: Each leg's quantity at the index price, in the quote currency.
    max_notional: float
    #: Today's live net P&L may fall this far before new orders are refused.
    daily_loss: float


class SharkExecutor:
    """Market orders on Shark's options, filled and read back."""

    venue = "shark_options"

    def __init__(
        self,
        account: SharkOptionsAccount,
        market: PaperMarket,
        *,
        wait: float = FILL_WAIT,
        sleep: Callable[[float], None] = clock.sleep,
    ) -> None:
        self._account = account
        self._market = market
        self._wait = wait
        self._sleep = sleep

    def _find(self, order_id: str) -> OptionsOrder | None:
        return next(
            (o for o in self._account.recent_orders() if o.client_order_id == order_id), None
        )

    def execute(
        self, symbol: str, side: Literal["buy", "sell"], qty: float, now: datetime
    ) -> Execution:
        placed = self._account.place_market(symbol, side, qty)
        order_id = placed.client_order_id
        seen: OptionsOrder | None = placed
        deadline = clock.monotonic() + self._wait
        while True:
            if seen is not None and seen.status in DONE and seen.avg_price:
                break
            if seen is not None and seen.status in DEAD:
                raise LiveError(
                    f"Shark {seen.status.lower()} the {side} of {qty:g} {symbol}; nothing filled"
                )
            if clock.monotonic() >= deadline:
                break
            self._sleep(0.4)
            seen = self._find(order_id)

        if seen is None or seen.status not in DONE or not seen.avg_price:
            # Not filled in time: cancel what is left, then say exactly what stands.
            try:
                self._account.cancel(order_id)
            except BrokerError as exc:
                log.warning("cancel %s: %s", order_id, exc)
            final = self._find(order_id)
            filled = final.filled if final is not None else 0.0
            if final is not None and filled > 0 and final.avg_price:
                raise LiveError(
                    f"only {filled:g} of {qty:g} {symbol} filled at {final.avg_price:g} before the "
                    f"rest was cancelled (order {order_id}) - that part is open on Shark"
                )
            raise LiveError(
                f"the {side} of {qty:g} {symbol} did not fill and was cancelled (order {order_id})"
            )

        price = seen.avg_price
        filled = seen.filled or qty
        fee = self._market.fee(symbol, price, filled, buy=side == "buy", at=now)
        return Execution(price=price, qty=filled, fee=fee, order_id=order_id)


def check(
    conn: sqlite3.Connection,
    market: PaperMarket,
    limits: Limits,
    orders: list[desk.Priced],
    underlying: str,
    now: datetime,
    *,
    available: Callable[[], float] | None = None,
) -> None:
    """Refuse a live basket that breaks a limit, before anything is sent."""
    if not limits.enabled:
        raise PaperError("live trading is off - set SHARK_OPTIONS_LIVE=true to allow real orders")
    index = market.spot(underlying)
    if index is None:
        raise PaperError(f"no {underlying} index price to check the order against")
    for o in orders:
        notional = o.qty * index
        if notional > limits.max_notional:
            raise PaperError(
                f"{o.symbol}: {o.qty:g} is {notional:,.0f} of notional, over the "
                f"{limits.max_notional:,.0f} cap (SHARK_MAX_NOTIONAL)"
            )
    today = now.astimezone(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    net = 0.0
    for leg in paper_repo.live_legs_since(conn, today):
        v = desk.value(market, leg, now)
        net += v.net or 0.0
    if net <= -limits.daily_loss:
        raise PaperError(
            f"today's live options are down {-net:,.2f}, past the {limits.daily_loss:,.2f} daily "
            "limit (SHARK_OPTIONS_DAILY_LOSS)"
        )
    if available is not None:
        held = []
        for o in orders:
            c = market.contract(o.symbol)
            if c is None:
                raise PaperError(f"{o.symbol} is not listed")
            held.append(Held(c.kind, o.side, c.strike, c.expiry, c.delivery, o.qty, o.price))
        paid = sum(o.price * o.qty for o in orders if o.side == "buy")
        needed = market.margin(held, underlying, now) + paid + sum(o.fee for o in orders)
        have = available()
        if needed > have:
            raise PaperError(
                f"the basket needs about {needed:,.2f} and the options wallet has {have:,.2f} free"
            )


def wallet_usdt(broker: SharkOptionsBroker, account: SharkOptionsAccount) -> float:
    """What the options wallet has free, in USDT: it is held in rupees."""
    w = account.wallet()
    if w.margin_asset.upper() == "USDT":
        return w.available
    return w.available / broker.conversion_rate()
