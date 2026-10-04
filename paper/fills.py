"""What a paper order would really have got, and what it would have cost.

A fill walks the order book on the side that takes it - a buy lifts the asks, a
sell hits the bids - level by level until the quantity is done, and is priced at
the average. Not the mark, and not the top of the book alone: Shark's books are
thin away from the money, and a paper fill that assumes depth that is not there
makes every result look better than trading it would have been.

Fees are the venue's own terms, from its base pairs: a percentage of the
underlying's notional, capped at a percentage of the premium traded. Delivery -
settlement at expiry - is charged the contract's own rate on the same footing.
That the cap applies to delivery as well is an assumption: the venue publishes
one cap, beside the trading fees.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from broker.shark.options_parse import BasePair, BookLevel, OrderBook

#: Book sizes are decimals; a quantity summed from them is not exact.
EPS = 1e-9


class NotEnoughBook(ValueError):
    """The book on that side holds less than the quantity asked for."""


@dataclass(frozen=True)
class Fill:
    price: float
    qty: float
    #: How far the average is from the best level - what depth cost.
    levels: int


def walk(levels: list[BookLevel], qty: float) -> Fill:
    """The average price of taking `qty` from `levels`, best first."""
    if qty <= 0:
        raise ValueError("a fill needs a quantity")
    left = qty
    cost = 0.0
    used = 0
    for level in levels:
        take = min(left, level.size)
        cost += take * level.price
        left -= take
        used += 1
        if left <= EPS:
            return Fill(price=cost / qty, qty=qty, levels=used)
    available = qty - left
    raise NotEnoughBook(
        f"only {available:g} on the book, {qty:g} asked" if available > 0 else "nothing on the book"
    )


def take(book: OrderBook, side: Literal["buy", "sell"], qty: float) -> Fill:
    """Fill a market order: a buy against the asks, a sell against the bids."""
    return walk(book.asks if side == "buy" else book.bids, qty)


def trade_fee(pair: BasePair, index: float, price: float, qty: float) -> float:
    """A taker's fee: a share of the underlying's notional, capped by the premium."""
    on_notional = pair.taker_fee_pct / 100 * index * qty
    cap = pair.fee_cap_pct / 100 * price * qty
    return min(on_notional, cap)


def delivery_fee(
    pair: BasePair, rate_pct: float, index: float, settle_value: float, qty: float
) -> float:
    """Settling at expiry. Nothing on a contract that expires worthless."""
    if settle_value <= 0:
        return 0.0
    return min(rate_pct / 100 * index * qty, pair.fee_cap_pct / 100 * settle_value * qty)


def settle_value(kind: Literal["CE", "PE"], strike: float, at: float) -> float:
    """What a contract is worth at expiry: its intrinsic value against `at`."""
    return max(at - strike, 0.0) if kind == "CE" else max(strike - at, 0.0)


def margin(
    pair: BasePair,
    *,
    kind: Literal["CE", "PE"],
    side: Literal["buy", "sell"],
    strike: float,
    index: float,
    mark: float,
    qty: float,
) -> float:
    """What a position ties up, the way options venues of this shape compute it.

    A long has paid its premium and needs nothing more. A short posts the larger
    of the maximum factor of the index less how far out of the money it is, and
    the minimum factor of the index - plus its own mark - per unit. An estimate:
    the venue's own figure is in its wallet, which needs a key to read.
    """
    if side == "buy":
        return 0.0
    otm = max(strike - index, 0.0) if kind == "CE" else max(index - strike, 0.0)
    per = max(pair.max_im_pct / 100 * index - otm, pair.min_im_pct / 100 * index) + mark
    return per * qty
