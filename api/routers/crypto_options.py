"""The crypto options desk's market: BTC and ETH options on Shark.

Its own router rather than `?venue=` on the NSE desk's, for the reason the perps
desk has one: the vocabulary differs. Quantities are fractions of a coin, not
lots; the market never closes; and the fee and margin terms are the venue's own.
What it shares with the NSE desk is the chain's shape, which is reused as is.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from api.deps import CryptoOptionsDep
from broker.models import Expiry, OptionChain

router = APIRouter(tags=["crypto-options"], prefix="/api/crypto-options")


class UnderlyingResponse(BaseModel):
    underlying: str
    quote: str
    #: The spot index, when the feed has carried one.
    spot: float | None
    maker_fee_pct: float
    taker_fee_pct: float
    fee_cap_pct: float
    min_im_pct: float
    max_im_pct: float
    mm_pct: float
    #: The size an order moves in, in the coin: 0.01 BTC. One "lot" in a leg.
    qty_step: float
    min_qty: float


@router.get("/underlyings", response_model=list[UnderlyingResponse])
def underlyings(broker: CryptoOptionsDep) -> list[UnderlyingResponse]:
    out = []
    for p in broker.base_pairs():
        listed = broker.instruments(f"{p.underlying}-{p.quote}")
        if not listed:
            continue
        out.append(
            UnderlyingResponse(
                underlying=p.underlying,
                quote=p.quote,
                spot=broker.spot(f"{p.underlying}-{p.quote}"),
                maker_fee_pct=p.maker_fee_pct,
                taker_fee_pct=p.taker_fee_pct,
                fee_cap_pct=p.fee_cap_pct,
                min_im_pct=p.min_im_pct,
                max_im_pct=p.max_im_pct,
                mm_pct=p.mm_pct,
                qty_step=max(i.qty_step for i in listed),
                min_qty=max(i.min_qty for i in listed),
            )
        )
    return out


@router.get("/expiries", response_model=list[Expiry])
def expiries(broker: CryptoOptionsDep, underlying: str = "BTC") -> list[Expiry]:
    return broker.expiries(underlying)


@router.get("/chain", response_model=OptionChain)
def chain(
    broker: CryptoOptionsDep, underlying: str = "BTC", expiry: str = "", strikes: int = 15
) -> OptionChain:
    """One expiry's chain. `expiry` is a token from `/expiries`; empty is the nearest."""
    return broker.get_option_chain(underlying, strike_count=strikes, expiry_token=expiry)


class LevelResponse(BaseModel):
    price: float
    size: float


class BookResponse(BaseModel):
    symbol: str
    bids: list[LevelResponse]
    asks: list[LevelResponse]


@router.get("/book", response_model=BookResponse)
def book(broker: CryptoOptionsDep, symbol: str) -> BookResponse:
    found = broker.order_book(symbol)
    return BookResponse(
        symbol=found.symbol,
        bids=[LevelResponse(price=lv.price, size=lv.size) for lv in found.bids],
        asks=[LevelResponse(price=lv.price, size=lv.size) for lv in found.asks],
    )
