"""Shark's options wire format, turned into the desk's models.

The options book is a different service from the perpetuals one - its own REST
host and its own socket - and a different shape: Bybit-like rather than
Binance-like, camelCase fields with numbers as strings. Nothing here is in the
venue's published docs; it was read off the endpoints the venue's own web app
calls, so every field is read defensively and a missing price stays missing
rather than becoming a zero the desk would act on.

A contract is spelled `BTC-5OCT26-85500-P-USDT`: underlying, expiry as day,
month and two-digit year (no leading zero on the day), strike, C or P, quote.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from broker.models import Expiry, Greeks, OptionType
from broker.shark.parse import SharkParseError

_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


def _num(value: Any, field: str) -> float:
    if value is None or value == "":
        raise SharkParseError(f"missing {field}")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise SharkParseError(f"{field} is not a number: {value!r}") from exc


def _opt(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _price(value: Any) -> float | None:
    """A price, or None for absent or zero - an empty side of the book reads as 0."""
    number = _opt(value)
    return number if number is not None and number > 0 else None


def expiry_code(day: date) -> str:
    """`2026-10-05` -> `"5OCT26"`, the expiry as a contract symbol spells it."""
    return f"{day.day}{_MONTHS[day.month - 1]}{day.year % 100:02d}"


def parse_expiry_code(code: str) -> date | None:
    """`"5OCT26"` -> `2026-10-05`, or None if it is not that shape."""
    if len(code) < 6:
        return None
    day, month, year = code[:-5], code[-5:-2], code[-2:]
    if month not in _MONTHS or not day.isdigit() or not year.isdigit():
        return None
    try:
        return date(2000 + int(year), _MONTHS.index(month) + 1, int(day))
    except ValueError:
        return None


@dataclass(frozen=True)
class ContractName:
    underlying: str
    expiry: date
    strike: float
    option_type: OptionType
    quote: str

    @property
    def series(self) -> str:
        """Underlying and expiry: two contracts with one series share an expiry."""
        return f"{self.underlying}-{expiry_code(self.expiry)}"


def parse_symbol(symbol: str) -> ContractName | None:
    """A contract symbol's parts, or None if it is not an option symbol."""
    parts = symbol.split("-")
    if len(parts) != 5:
        return None
    underlying, code, strike, kind, quote = parts
    expiry = parse_expiry_code(code)
    if expiry is None or kind not in ("C", "P"):
        return None
    try:
        strike_value = float(strike)
    except ValueError:
        return None
    return ContractName(underlying, expiry, strike_value, "CE" if kind == "C" else "PE", quote)


def topic_prefix(underlying: str, quote: str, expiry: date) -> str:
    """`BTC_USDT_5OCT26` - how the socket names one expiry's chain."""
    return f"{underlying}_{quote}_{expiry_code(expiry)}"


@dataclass(frozen=True)
class Instrument:
    """One listed option, as the venue's catalogue describes it."""

    symbol: str
    underlying: str
    quote: str
    #: What the account is debited in - INR, though prices are in the quote.
    settle: str
    strike: float
    option_type: OptionType
    #: When it settles. 08:00 UTC on every contract seen so far.
    delivery: datetime
    tick_size: float
    #: Quantity steps are in the underlying: 0.01 BTC, not a whole contract.
    qty_step: float
    min_qty: float
    max_qty: float
    #: A fee on settlement, as a fraction of the settled value.
    delivery_fee_rate: float
    #: Prices the catalogue carries, which are a snapshot from when it was built.
    last: float | None
    mark: float | None


def parse_instruments(rows: Any) -> list[Instrument]:
    if not isinstance(rows, list):
        raise SharkParseError("instrument info is not a list")
    out: list[Instrument] = []
    for row in rows:
        kind = row.get("optionsType")
        if kind not in ("Call", "Put"):
            raise SharkParseError(f"unknown optionsType {kind!r}")
        price = row.get("priceFilter") or {}
        lots = row.get("lotSizeFilter") or {}
        out.append(
            Instrument(
                symbol=str(row["symbol"]),
                underlying=str(row["baseCoin"]),
                quote=str(row["quoteCoin"]),
                settle=str(row.get("settleCoin") or row["quoteCoin"]),
                strike=_num(row.get("strikePrice"), "strikePrice"),
                option_type="CE" if kind == "Call" else "PE",
                delivery=datetime.fromtimestamp(
                    _num(row.get("deliveryTime"), "deliveryTime") / 1000, tz=UTC
                ),
                tick_size=_num(price.get("tickSize"), "tickSize"),
                qty_step=_num(lots.get("qtyStep"), "qtyStep"),
                min_qty=_num(lots.get("minOrderQty"), "minOrderQty"),
                max_qty=_num(lots.get("maxOrderQty"), "maxOrderQty"),
                delivery_fee_rate=_opt(row.get("deliveryFeeRate")) or 0.0,
                last=_price(row.get("lastPrice")),
                mark=_price(row.get("markPrice")),
            )
        )
    return out


def parse_delivery_times(rows: Any) -> list[datetime]:
    if not isinstance(rows, list):
        raise SharkParseError("delivery times is not a list")
    return sorted(datetime.fromtimestamp(_num(ms, "deliveryTime") / 1000, tz=UTC) for ms in rows)


def to_expiries(deliveries: list[datetime]) -> list[Expiry]:
    """The listed expiries, in the shape the desk's chain carries.

    `weekly` marks every expiry but the last of its month, which is how the NSE
    desk reads the word: the monthly is the month's final one. The token is the
    delivery instant in milliseconds, the venue's own key for an expiry.
    """
    last_in_month: dict[tuple[int, int], datetime] = {}
    for at in deliveries:
        key = (at.year, at.month)
        last_in_month[key] = max(at, last_in_month.get(key, at))
    return [
        Expiry(
            date=at.strftime("%d-%m-%Y"),
            token=str(int(at.timestamp() * 1000)),
            weekly=last_in_month[(at.year, at.month)] != at,
        )
        for at in deliveries
    ]


@dataclass(frozen=True)
class OptionTicker:
    """One contract's live state, from the socket's `ticker` event.

    Every frame is a full snapshot - all fields, every time - so the latest one
    is the contract's state and nothing has to be merged.
    """

    symbol: str
    bid: float | None
    bid_size: float
    ask: float | None
    ask_size: float
    last: float | None
    mark: float | None
    #: The venue's IVs, as fractions (0.2067 is 20.67%).
    mark_iv: float | None
    bid_iv: float | None
    ask_iv: float | None
    delta: float | None
    gamma: float | None
    vega: float | None
    theta: float | None
    #: The spot index, and the futures price for this expiry the venue prices
    #: options off. The two differ by the basis.
    index: float | None
    underlying: float | None
    open_interest: float
    volume_24h: float
    change_24h: float
    received_at: datetime

    @property
    def greeks(self) -> Greeks | None:
        if None in (self.delta, self.gamma, self.theta, self.vega, self.mark_iv):
            return None
        assert self.delta is not None and self.gamma is not None
        assert self.theta is not None and self.vega is not None and self.mark_iv is not None
        return Greeks(
            delta=self.delta, gamma=self.gamma, theta=self.theta, vega=self.vega, iv=self.mark_iv
        )


def parse_option_ticker(payload: Any, received_at: datetime | None = None) -> OptionTicker | None:
    """One ticker frame, or None if it is not one.

    None rather than an exception: this runs in a socket callback, and one bad
    frame should cost that frame, not the connection.
    """
    if not isinstance(payload, dict) or not payload.get("symbol"):
        return None
    return OptionTicker(
        symbol=str(payload["symbol"]),
        bid=_price(payload.get("bidPrice")),
        bid_size=_opt(payload.get("bidSize")) or 0.0,
        ask=_price(payload.get("askPrice")),
        ask_size=_opt(payload.get("askSize")) or 0.0,
        last=_price(payload.get("lastPrice")),
        mark=_price(payload.get("markPrice")),
        mark_iv=_opt(payload.get("markPriceIv")),
        bid_iv=_opt(payload.get("bidIv")),
        ask_iv=_opt(payload.get("askIv")),
        delta=_opt(payload.get("delta")),
        gamma=_opt(payload.get("gamma")),
        vega=_opt(payload.get("vega")),
        theta=_opt(payload.get("theta")),
        index=_price(payload.get("indexPrice")),
        underlying=_price(payload.get("underlyingPrice")),
        open_interest=_opt(payload.get("openInterest")) or 0.0,
        volume_24h=_opt(payload.get("volume24h")) or 0.0,
        change_24h=_opt(payload.get("change24h")) or 0.0,
        received_at=received_at or datetime.now(UTC),
    )


@dataclass(frozen=True)
class BookLevel:
    price: float
    size: float


@dataclass(frozen=True)
class OrderBook:
    """Resting orders on one contract, best first on each side."""

    symbol: str
    bids: list[BookLevel]
    asks: list[BookLevel]


def parse_order_book(payload: Any) -> OrderBook:
    """The book, sorted best first - the venue's order is not relied on.

    The socket sends asks highest first and the REST endpoint lowest first, so
    sorting here is what lets both feed the same reader.
    """
    if not isinstance(payload, dict) or "symbol" not in payload:
        raise SharkParseError("order book has no symbol")

    def side(rows: Any, field: str) -> list[BookLevel]:
        if not isinstance(rows, list):
            raise SharkParseError(f"order book {field} is not a list")
        levels = [BookLevel(_num(p, f"{field} price"), _num(q, f"{field} size")) for p, q in rows]
        return [lv for lv in levels if lv.price > 0 and lv.size > 0]

    bids = sorted(side(payload.get("bids", []), "bids"), key=lambda lv: -lv.price)
    asks = sorted(side(payload.get("asks", []), "asks"), key=lambda lv: lv.price)
    return OrderBook(symbol=str(payload["symbol"]), bids=bids, asks=asks)


class SharkOptionSymbols:
    """The `ContractCodec` for Shark's option symbols. See `broker/contracts.py`."""

    def parse_contract(self, symbol: str) -> tuple[str, float, OptionType] | None:
        name = parse_symbol(symbol)
        return None if name is None else (name.series, name.strike, name.option_type)

    def series_prefix(self, symbol: str, strike: float) -> str | None:
        name = parse_symbol(symbol)
        return None if name is None or name.strike != strike else name.series

    def expiry_for_symbol(self, symbol: str, expiries: list[Expiry]) -> Expiry | None:
        name = parse_symbol(symbol)
        if name is None:
            return None
        wanted = name.expiry.strftime("%d-%m-%Y")
        return next((e for e in expiries if e.date == wanted), None)

    def futures_symbol(self, option_symbol: str, strike: float) -> str | None:
        # The venue prices each expiry off a futures price it publishes, but
        # lists no dated future to trade.
        return None


SYMBOLS = SharkOptionSymbols()


@dataclass(frozen=True)
class BasePair:
    """An underlying the venue lists options on, and the terms it trades them on.

    Fees are percentages of the underlying's notional, capped at a percentage of
    the option's own value - so a far, cheap option pays the cap rather than a
    fee larger than its premium. Margin factors are percentages of the notional.
    """

    underlying: str
    quote: str
    maker_fee_pct: float
    taker_fee_pct: float
    #: The cap: a fee never exceeds this percentage of the premium traded.
    fee_cap_pct: float
    min_im_pct: float
    max_im_pct: float
    mm_pct: float
    price_precision: int


def parse_base_pairs(rows: Any) -> list[BasePair]:
    if not isinstance(rows, list):
        raise SharkParseError("base pairs is not a list")
    return [
        BasePair(
            underlying=str(row["baseCoin"]),
            quote=str(row["quoteCoin"]),
            maker_fee_pct=_num(row.get("makerFeePercentage"), "makerFeePercentage"),
            taker_fee_pct=_num(row.get("takerFeePercentage"), "takerFeePercentage"),
            fee_cap_pct=_num(
                row.get("maxProportionOfTxnInOrderPricePercentage"),
                "maxProportionOfTxnInOrderPricePercentage",
            ),
            min_im_pct=_num(row.get("minImFactorPercentage"), "minImFactorPercentage"),
            max_im_pct=_num(row.get("maxImFactorPercentage"), "maxImFactorPercentage"),
            mm_pct=_num(row.get("mmFactorPercentage"), "mmFactorPercentage"),
            price_precision=int(row.get("pricePrecision") or 2),
        )
        for row in rows
    ]
