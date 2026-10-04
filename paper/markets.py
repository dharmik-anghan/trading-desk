"""The live option markets a paper trade can be placed on, behind one shape.

Two today, and they differ in almost everything a fill depends on:

- **Shark** (BTC, ETH): the whole order book is public, so a fill walks it.
  Quantities are fractions of a coin; fees are the venue's, a share of the
  underlying's notional capped by the premium. Never closes.
- **The NSE** through Fyers (NIFTY and the rest): the chain carries only the
  best bid and ask, so a fill takes the top of the book whatever the size - an
  assumption a large order would not survive. Quantities are whole lots; charges
  are the backtest's (brokerage and the dated statutory rates). Trades 09:15 to
  15:30 IST on sessions, and settles at the close.

Everything above this module - the desk, the watcher, the page - asks these
questions and never which venue it is talking to.
"""

from __future__ import annotations

import math
import time as clock
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Literal, Protocol

from analytics import black_scholes as bs
from broker.base import MarketData, OptionsData
from broker.contracts import ContractCodec
from broker.errors import BrokerError
from broker.models import Expiry, OptionChain, OptionChainRow
from broker.shark.options import SharkOptionsBroker
from broker.shark.options_parse import BasePair, parse_symbol
from optbt.costs import CostModel
from optbt.data.history import KNOWN_LOT_SIZES, _vote_lot
from optbt.margin import MarginLeg, estimate
from optbt.marks import implied_vol
from paper import fills
from venues.calendar import IST, NSE_CLOSE, in_session
from venues.instruments import OPTION_SERIES

Side = Literal["buy", "sell"]
Kind = Literal["CE", "PE"]

YEAR = 365 * 24 * 3600


class PaperError(ValueError):
    """An order the desk will not take, with why."""


@dataclass(frozen=True)
class Contract:
    symbol: str
    underlying: str
    kind: Kind
    strike: float
    expiry: date
    #: When it settles.
    delivery: datetime
    #: Orders move in steps of this, in units: 0.01 BTC, or one NIFTY lot (65).
    step: float
    min_qty: float
    max_qty: float


@dataclass(frozen=True)
class LiveQuote:
    bid: float | None
    ask: float | None
    #: The venue's mark where it has one; the mid, else the last trade, where not.
    mark: float | None
    ltp: float | None
    iv: float | None
    delta: float | None
    oi: float
    volume: float


@dataclass(frozen=True)
class Held:
    """An open leg, as a margin estimate needs it."""

    kind: Kind
    side: Side
    strike: float
    expiry: date
    delivery: datetime
    qty: float
    #: Its price now; the entry, when there is none.
    mark: float


@dataclass(frozen=True)
class LiveExpiry:
    expiry: date
    token: str
    monthly: bool
    delivery: datetime


@dataclass(frozen=True)
class LiveRow:
    strike: float
    ce: tuple[str, LiveQuote] | None
    pe: tuple[str, LiveQuote] | None


@dataclass(frozen=True)
class LiveChain:
    underlying: str
    venue: str
    #: What premiums are in, and what money - P&L, fees - is in.
    currency: str
    spot: float
    #: The futures price the expiry is priced off, where the venue says.
    forward: float
    #: What "at the money" is measured from when a template picks strikes:
    #: spot on the NSE, as the backtest does; the expiry's forward on Shark,
    #: which is what its chain is centred on.
    reference: float
    expiries: list[LiveExpiry]
    expiry: LiveExpiry
    step: float
    min_qty: float
    rows: list[LiveRow]
    fetched_at: datetime
    #: Whether orders can be filled now.
    open: bool


@dataclass(frozen=True)
class Execution:
    """A real order's fill, as the venue reported it."""

    price: float
    qty: float
    fee: float
    order_id: str


class Executor(Protocol):
    """Sends real orders on one venue. What makes a session live."""

    venue: str

    def execute(self, symbol: str, side: Side, qty: float, now: datetime) -> Execution: ...


class PaperMarket(Protocol):
    venue: str
    currency: str

    def underlyings(self) -> list[str]: ...

    def chain(self, underlying: str, expiry_token: str, strikes: int) -> LiveChain: ...

    def contract(self, symbol: str) -> Contract | None: ...

    def quote(self, symbol: str) -> LiveQuote | None: ...

    def spot(self, underlying: str) -> float | None: ...

    def is_open(self, at: datetime) -> bool: ...

    def fill(self, symbol: str, side: Side, qty: float) -> float: ...

    def fee(self, symbol: str, price: float, qty: float, *, buy: bool, at: datetime) -> float: ...

    def settle_fee(
        self, symbol: str, value: float, qty: float, *, long: bool, at: datetime
    ) -> float: ...

    def margin(self, legs: list[Held], underlying: str, at: datetime) -> float: ...


def years_left(delivery: datetime, at: datetime) -> float:
    return max((delivery - at).total_seconds() / YEAR, 0.0)


# ------------------------------------------------------------------- Shark


class SharkMarket:
    venue = "shark_options"
    currency = "USDT"

    def __init__(self, broker: SharkOptionsBroker) -> None:
        self._b = broker

    @property
    def broker(self) -> SharkOptionsBroker:
        return self._b

    def underlyings(self) -> list[str]:
        return [p.underlying for p in self._b.base_pairs()]

    def chain(self, underlying: str, expiry_token: str, strikes: int) -> LiveChain:
        chain = self._b.get_option_chain(
            underlying, strike_count=strikes, expiry_token=expiry_token
        )
        base, _quote = chain.underlying_symbol.split("-")
        listed = self._b.instruments(chain.underlying_symbol)
        expiries = [
            LiveExpiry(
                expiry=_day(e.token),
                token=e.token,
                monthly=not e.weekly,
                delivery=datetime.fromtimestamp(int(e.token) / 1000, tz=UTC),
            )
            for e in chain.expiries
        ]
        chosen = next(e for e in expiries if e.token == chain.expiry_token)
        spot = self._b.spot(chain.underlying_symbol) or chain.underlying_ltp
        return LiveChain(
            underlying=base,
            venue=self.venue,
            currency=self.currency,
            spot=spot,
            forward=chain.underlying_ltp,
            reference=chain.underlying_ltp,
            expiries=expiries,
            expiry=chosen,
            step=max((i.qty_step for i in listed), default=0.01),
            min_qty=max((i.min_qty for i in listed), default=0.01),
            rows=_rows(chain, _shark_quote),
            fetched_at=chain.fetched_at,
            open=True,
        )

    def contract(self, symbol: str) -> Contract | None:
        i = self._b.instrument(symbol)
        if i is None:
            return None
        return Contract(
            symbol=symbol,
            underlying=i.underlying,
            kind=i.option_type,
            strike=i.strike,
            expiry=i.delivery.date(),
            delivery=i.delivery,
            step=i.qty_step,
            min_qty=i.min_qty,
            max_qty=i.max_qty,
        )

    def quote(self, symbol: str) -> LiveQuote | None:
        t = self._b.ticker(symbol)
        if t is None:
            return None
        return LiveQuote(
            bid=t.bid,
            ask=t.ask,
            mark=t.mark,
            ltp=t.last,
            iv=t.mark_iv,
            delta=t.delta,
            oi=t.open_interest,
            volume=t.volume_24h,
        )

    def spot(self, underlying: str) -> float | None:
        return self._b.spot(underlying)

    def is_open(self, at: datetime) -> bool:
        return True

    def fill(self, symbol: str, side: Side, qty: float) -> float:
        try:
            return fills.take(self._b.order_book(symbol), side, qty).price
        except fills.NotEnoughBook as exc:
            raise PaperError(f"cannot {side} {qty:g}: {exc}") from exc

    def _index(self, symbol: str) -> float | None:
        name = parse_symbol(symbol)
        if name is None:
            return None
        return self._b.spot(f"{name.underlying}-{name.quote}")

    def _pair(self, symbol: str) -> BasePair:
        name = parse_symbol(symbol)
        if name is None:
            raise PaperError(f"not a Shark option: {symbol}")
        return self._b.base_pair(f"{name.underlying}-{name.quote}")

    def fee(self, symbol: str, price: float, qty: float, *, buy: bool, at: datetime) -> float:
        index = self._index(symbol)
        return 0.0 if index is None else fills.trade_fee(self._pair(symbol), index, price, qty)

    def settle_fee(
        self, symbol: str, value: float, qty: float, *, long: bool, at: datetime
    ) -> float:
        index = self._index(symbol)
        listed = self._b.instrument(symbol)
        if index is None or listed is None:
            return 0.0
        return fills.delivery_fee(self._pair(symbol), listed.delivery_fee_rate, index, value, qty)

    def margin(self, legs: list[Held], underlying: str, at: datetime) -> float:
        index = self._b.spot(underlying)
        if index is None or not legs:
            return 0.0
        pair = self._b.base_pair(underlying)
        return sum(
            fills.margin(
                pair, kind=h.kind, side=h.side, strike=h.strike, index=index, mark=h.mark, qty=h.qty
            )
            for h in legs
        )


def _day(token: str) -> date:
    return datetime.fromtimestamp(int(token) / 1000, tz=UTC).date()


def _shark_quote(r: OptionChainRow) -> LiveQuote:
    return LiveQuote(
        bid=r.bid or None,
        ask=r.ask or None,
        mark=r.mark if r.mark is not None else (r.ltp or None),
        ltp=r.ltp or None,
        iv=r.greeks.iv if r.greeks else None,
        delta=r.greeks.delta if r.greeks else None,
        oi=r.oi,
        volume=r.volume,
    )


def _rows(chain: OptionChain, make: Callable[[OptionChainRow], LiveQuote]) -> list[LiveRow]:
    by: dict[float, dict[str, tuple[str, LiveQuote]]] = {}
    for r in chain.rows:
        by.setdefault(r.strike, {})[r.option_type] = (r.symbol, make(r))
    return [LiveRow(k, v.get("CE"), v.get("PE")) for k, v in sorted(by.items())]


# --------------------------------------------------------------------- NSE


#: How long an NSE chain is reused: the Fyers adapter caches too, and the
#: watcher reads quotes rather than chains.
NSE_CHAIN_TTL = 2.0


class NseMarket:
    """NSE index options through Fyers' live chain."""

    venue = "fyers"
    currency = "INR"

    def __init__(
        self,
        broker: OptionsData,
        codec: ContractCodec,
        quotes: MarketData,
        costs: CostModel | None = None,
        holidays: Callable[[], frozenset[date]] = frozenset,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._b = broker
        self._codec = codec
        # get_quote(symbols) -> dict[str, Quote]; the same adapter, typed apart
        # because OptionsData alone does not promise it.
        self._quotes = quotes
        self._costs = costs or CostModel()
        self._holidays = holidays
        self._now = clock
        #: symbol -> contract, filled from every chain read.
        self._contracts: dict[str, Contract] = {}
        self._lots: dict[tuple[str, date], int] = {}
        self._chains: dict[tuple[str, str], tuple[float, LiveChain]] = {}

    def underlyings(self) -> list[str]:
        return list(OPTION_SERIES)

    def _underlying_of(self, symbol: str) -> str | None:
        body = symbol.split(":", 1)[-1]
        for name in sorted(OPTION_SERIES, key=len, reverse=True):
            if body.startswith(name):
                return name
        return None

    def chain(self, underlying: str, expiry_token: str, strikes: int) -> LiveChain:
        key = (underlying, expiry_token)
        held = self._chains.get(key)
        if held is not None and clock.monotonic() - held[0] < NSE_CHAIN_TTL:
            return held[1]
        index = OPTION_SERIES.get(underlying)
        if index is None:
            raise PaperError(f"no NSE options on {underlying}")
        raw = self._b.get_option_chain(index, strike_count=strikes, expiry_token=expiry_token)
        expiries = [
            LiveExpiry(
                expiry=(d := datetime.strptime(e.date, "%d-%m-%Y").date()),
                token=e.token,
                monthly=not e.weekly,
                delivery=datetime.combine(d, NSE_CLOSE, tzinfo=IST),
            )
            for e in raw.expiries
        ]
        chosen = next((e for e in expiries if e.token == raw.expiry_token), None)
        if chosen is None:
            if not expiries:
                raise BrokerError(f"Fyers lists no {underlying} expiries")
            chosen = expiries[0]
        spot = raw.underlying_ltp
        lot = self._lot(underlying, chosen.expiry, raw)
        now = self._now()
        quotes = {r.symbol: r for r in raw.rows}
        forward = _forward(raw.rows, spot)
        years = years_left(chosen.delivery, now)

        def make(r: OptionChainRow) -> LiveQuote:
            bid = r.bid or None
            ask = r.ask or None
            mark = (bid + ask) / 2 if bid and ask else (r.ltp or None)
            iv = delta = None
            if r.greeks is not None:
                # Fyers states IV in percent (12.5); everything here is a fraction.
                iv, delta = r.greeks.iv / 100, r.greeks.delta
            elif mark and years > 0:
                iv = implied_vol(mark, forward, r.strike, years, r.option_type)
                if iv is not None:
                    delta = bs.greeks(forward, r.strike, 0.0, iv, years, r.option_type).delta
            return LiveQuote(bid, ask, mark, r.ltp or None, iv, delta, r.oi, r.volume)

        for r in quotes.values():
            self._contracts[r.symbol] = Contract(
                symbol=r.symbol,
                underlying=underlying,
                kind=r.option_type,
                strike=r.strike,
                expiry=chosen.expiry,
                delivery=chosen.delivery,
                step=lot,
                min_qty=lot,
                max_qty=lot * 1800,
            )
        out = LiveChain(
            underlying=underlying,
            venue=self.venue,
            currency=self.currency,
            spot=spot,
            forward=forward,
            reference=spot,
            expiries=expiries,
            expiry=chosen,
            step=lot,
            min_qty=lot,
            rows=_rows(raw, make),
            fetched_at=raw.fetched_at,
            open=self.is_open(now),
        )
        self._chains[key] = (clock.monotonic(), out)
        if chosen.token != expiry_token:
            self._chains[(underlying, chosen.token)] = (clock.monotonic(), out)
        return out

    def _lot(self, underlying: str, expiry: date, chain: OptionChain) -> int:
        """The lot, read off the chain's own figures the way the backtest reads
        it off the store's: open interest and volume are whole lots."""
        key = (underlying, expiry)
        if key not in self._lots:
            known = KNOWN_LOT_SIZES.get(underlying, frozenset())
            oi = [r.oi for r in chain.rows if r.oi > 0]
            volume = [r.volume for r in chain.rows if r.volume > 0]
            lot = _vote_lot((oi, volume), known)
            if lot is None:
                raise PaperError(f"cannot tell {underlying}'s lot size from the chain")
            self._lots[key] = lot
        return self._lots[key]

    def contract(self, symbol: str) -> Contract | None:
        found = self._contracts.get(symbol)
        if found is not None:
            return found
        underlying = self._underlying_of(symbol)
        if underlying is None:
            return None
        # Not seen yet: find its expiry, and read that chain.
        first = self.chain(underlying, "", 1)
        listed = [_as_expiry(e) for e in first.expiries]
        match = self._codec.expiry_for_symbol(symbol, listed)
        if match is None:
            return None
        self.chain(underlying, match.token, 40)
        return self._contracts.get(symbol)

    def quote(self, symbol: str) -> LiveQuote | None:
        found = self._quotes.get_quote([symbol]).get(symbol)
        if found is None:
            return None
        bid = found.bid or None
        ask = found.ask or None
        mark = (bid + ask) / 2 if bid and ask else (found.ltp or None)
        return LiveQuote(bid, ask, mark, found.ltp or None, None, None, 0, found.volume)

    def spot(self, underlying: str) -> float | None:
        index = OPTION_SERIES.get(underlying)
        if index is None:
            return None
        found = self._quotes.get_quote([index]).get(index)
        return found.ltp if found is not None else None

    def is_open(self, at: datetime) -> bool:
        return in_session(at, self._holidays())

    def fill(self, symbol: str, side: Side, qty: float) -> float:
        q = self.quote(symbol)
        price = (q.ask if side == "buy" else q.bid) if q is not None else None
        if price is None:
            raise PaperError(f"no {'ask' if side == 'buy' else 'bid'} to {side} {symbol} at")
        return price

    def fee(self, symbol: str, price: float, qty: float, *, buy: bool, at: datetime) -> float:
        return self._costs.fill(at.astimezone(IST).date(), price, int(round(qty)), buy=buy).total

    def settle_fee(
        self, symbol: str, value: float, qty: float, *, long: bool, at: datetime
    ) -> float:
        if not long or value <= 0:
            return 0.0
        return self._costs.exercise(at.astimezone(IST).date(), value, int(round(qty))).total

    def margin(self, legs: list[Held], underlying: str, at: datetime) -> float:
        spot = self.spot(underlying)
        if spot is None or not legs:
            return 0.0
        margin_legs = [
            MarginLeg(
                option_type=h.kind,
                strike=h.strike,
                expiry=h.expiry,
                side=1 if h.side == "buy" else -1,
                quantity=int(round(h.qty)),
                price=h.mark,
                iv=None,
            )
            for h in legs
        ]
        years = {h.expiry: years_left(h.delivery, at) for h in legs}
        # 15%: what an NSE index option's volatility is near when its own price
        # gives none to read. The estimate's scenarios move it either way.
        return estimate(margin_legs, spot, at.astimezone(IST).date(), years, 0.15).total


def _as_expiry(e: LiveExpiry) -> Expiry:
    return Expiry(date=e.expiry.strftime("%d-%m-%Y"), token=e.token, weekly=not e.monthly)


def _forward(rows: list[OptionChainRow], spot: float) -> float:
    """The forward by put-call parity at the strike nearest spot, from mids."""
    mids: dict[tuple[float, str], float] = {}
    for r in rows:
        mid = (r.bid + r.ask) / 2 if r.bid and r.ask else r.ltp
        if mid:
            mids[(r.strike, r.option_type)] = mid
    both = {k for k, t in mids if t == "CE"} & {k for k, t in mids if t == "PE"}
    if not both:
        return spot
    k = min(both, key=lambda s: abs(s - spot))
    forward = mids[(k, "CE")] - mids[(k, "PE")] + k
    return forward if math.isfinite(forward) and forward > 0 else spot
