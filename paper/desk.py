"""The paper desk: opening and closing legs against a live market, and the
watcher's pass over everything still open.

Every function takes the market and the time it is acting at, rather than
reaching for a clock or a broker, so a test can run a whole stop-out against a
market it wrote down. The book of record is the database; nothing is held here.
Which venue a leg trades on is its session's.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from broker.errors import BrokerError
from paper import fills
from paper.markets import Executor, Held, LiveQuote, PaperError, PaperMarket
from storage import paper_repo
from storage.paper_repo import PaperLeg, PaperSession

log = logging.getLogger(__name__)

__all__ = ["PaperError"]


def _steps(qty: float, step: float) -> bool:
    """Whether `qty` is a whole number of `step`s."""
    n = qty / step
    return abs(n - round(n)) < 1e-6


@dataclass(frozen=True)
class Priced:
    """An order checked and priced, not yet booked."""

    symbol: str
    side: Literal["buy", "sell"]
    qty: float
    price: float
    fee: float


def price_order(
    market: PaperMarket,
    session: PaperSession,
    symbol: str,
    side: Literal["buy", "sell"],
    qty: float,
    now: datetime,
) -> Priced:
    """What a market order would fill at now, or why it would not."""
    c = market.contract(symbol)
    if c is None:
        raise PaperError(f"{symbol} is not listed")
    if c.underlying != session.underlying:
        raise PaperError(f"{symbol} is not on {session.underlying}")
    if c.delivery <= now:
        raise PaperError(f"{symbol} has expired")
    if not market.is_open(now):
        raise PaperError("the market is closed")
    if qty < c.min_qty - 1e-12 or not _steps(qty, c.step):
        raise PaperError(f"quantity must be at least {c.min_qty:g}, in steps of {c.step:g}")
    if qty > c.max_qty:
        raise PaperError(f"quantity is over the venue's {c.max_qty:g} an order")
    price = market.fill(symbol, side, qty)
    fee = market.fee(symbol, price, qty, buy=side == "buy", at=now)
    return Priced(symbol, side, qty, price, fee)


def book(
    conn: sqlite3.Connection,
    market: PaperMarket,
    session: PaperSession,
    order: Priced,
    now: datetime,
) -> PaperLeg:
    c = market.contract(order.symbol)
    assert c is not None
    return paper_repo.add_leg(
        conn,
        session.id,
        symbol=order.symbol,
        side=order.side,
        kind=c.kind,
        strike=c.strike,
        expiry=c.expiry,
        delivery=c.delivery,
        qty=order.qty,
        entry_at=now,
        entry_price=order.price,
        entry_fee=order.fee,
        entry_index=market.spot(c.underlying),
    )


def open_leg(
    conn: sqlite3.Connection,
    market: PaperMarket,
    session: PaperSession,
    symbol: str,
    side: Literal["buy", "sell"],
    qty: float,
    now: datetime,
) -> PaperLeg:
    """A market order, filled now."""
    return book(conn, market, session, price_order(market, session, symbol, side, qty, now), now)


def open_basket(
    conn: sqlite3.Connection,
    market: PaperMarket,
    session: PaperSession,
    orders: list[tuple[str, Literal["buy", "sell"], float]],
    now: datetime,
) -> list[PaperLeg]:
    """Several orders, all or none: each is priced before any is booked, so a leg
    the market cannot fill leaves nothing half-built behind it."""
    priced = [price_order(market, session, s, side, q, now) for s, side, q in orders]
    return [book(conn, market, session, p, now) for p in priced]


def open_live(
    conn: sqlite3.Connection,
    market: PaperMarket,
    executor: Executor,
    session: PaperSession,
    priced: list[Priced],
    now: datetime,
) -> tuple[list[PaperLeg], str | None]:
    """Send each order for real, in turn, and book what the venue filled.

    Not all-or-none, because a venue cannot be asked for that: if the second
    order of a straddle is refused, the first has already filled. So it stops at
    the first failure and returns what was booked with why it stopped - and the
    filled legs stay as positions to be exited, rather than being unwound by a
    reflex that would send more real orders into whatever just went wrong.
    """
    booked: list[PaperLeg] = []
    for order in priced:
        try:
            done = executor.execute(order.symbol, order.side, order.qty, now)
        except (PaperError, BrokerError) as exc:
            standing = (
                f"{len(booked)} of {len(priced)} legs were filled" if booked else "nothing filled"
            )
            return booked, f"{exc} - {standing}"
        real = Priced(order.symbol, order.side, done.qty, done.price, done.fee)
        leg = book(conn, market, session, real, now)
        paper_repo.set_entry_order(conn, leg.id, done.order_id)
        booked.append(leg)
    return booked, None


def exit_leg(
    conn: sqlite3.Connection,
    market: PaperMarket,
    leg: PaperLeg,
    now: datetime,
    reason: str = "exit",
    executor: Executor | None = None,
) -> PaperLeg:
    """Close an open leg at the market: a short buys back, a long sells. With an
    executor - a live session's - by a real order."""
    if not leg.open:
        raise PaperError("that leg is already closed")
    if not market.is_open(now):
        raise PaperError("the market is closed")
    closing: Literal["buy", "sell"] = "sell" if leg.side == "buy" else "buy"
    order = None
    if executor is not None:
        done = executor.execute(leg.symbol, closing, leg.qty, now)
        price, fee, order = done.price, done.fee, done.order_id
    else:
        price = market.fill(leg.symbol, closing, leg.qty)
        fee = market.fee(leg.symbol, price, leg.qty, buy=closing == "buy", at=now)
    paper_repo.close_leg(conn, leg.id, at=now, price=price, fee=fee, reason=reason, order=order)
    closed = paper_repo.get_leg(conn, leg.id)
    assert closed is not None
    return closed


def exit_all(
    conn: sqlite3.Connection,
    market: PaperMarket,
    session_id: int,
    now: datetime,
    reason: str = "exit",
    executor: Executor | None = None,
) -> list[str]:
    """Close every included open leg. Returns why any could not be closed."""
    problems = []
    for leg in paper_repo.legs(conn, session_id):
        if leg.open and leg.enabled:
            try:
                exit_leg(conn, market, leg, now, reason, executor)
            except (PaperError, BrokerError) as exc:
                problems.append(f"{leg.symbol}: {exc}")
    return problems


def settle(
    conn: sqlite3.Connection,
    market: PaperMarket,
    session: PaperSession,
    leg: PaperLeg,
    now: datetime,
) -> PaperLeg | None:
    """Settle a leg whose delivery has passed, at intrinsic against the index.

    The index when the watcher gets to it, not the venue's settlement price,
    which neither publishes anywhere this can read. The watcher runs every
    second, so that is the index at delivery whenever the desk was up then.
    """
    if not leg.open or now < leg.delivery:
        return None
    index = market.spot(session.underlying)
    if index is None:
        return None
    value = fills.settle_value(leg.kind, leg.strike, index)
    fee = market.settle_fee(leg.symbol, value, leg.qty, long=leg.side == "buy", at=now)
    paper_repo.close_leg(conn, leg.id, at=now, price=value, fee=fee, reason="expiry")
    return paper_repo.get_leg(conn, leg.id)


def triggered(leg: PaperLeg, quote: LiveQuote) -> str | None:
    """ "stop" or "target", if the price the leg would close at has reached one.

    Judged on the side of the book that would close it - a short buys back at
    the ask, a long sells at the bid - so a stop fires on a price that could be
    had, not on a mark nobody is quoting.
    """
    price = quote.ask if leg.side == "sell" else quote.bid
    if price is None:
        return None
    if leg.side == "sell":
        if leg.stop is not None and price >= leg.stop:
            return "stop"
        if leg.target is not None and price <= leg.target:
            return "target"
    else:
        if leg.stop is not None and price <= leg.stop:
            return "stop"
        if leg.target is not None and price >= leg.target:
            return "target"
    return None


@dataclass(frozen=True)
class Valued:
    """A leg as it stands: its mark, P&L and the fees it has cost or will."""

    leg: PaperLeg
    #: The market's mark for an open leg; the exit for a closed one.
    mark: float | None
    bid: float | None
    ask: float | None
    iv: float | None
    gross: float | None
    #: Paid, and for an open leg what closing at the mark would add.
    fees: float

    @property
    def net(self) -> float | None:
        return None if self.gross is None else self.gross - self.fees


def value(
    market: PaperMarket, leg: PaperLeg, now: datetime, quote: LiveQuote | None = None
) -> Valued:
    if not leg.open:
        assert leg.exit_price is not None
        booked = leg.sign * (leg.exit_price - leg.entry_price) * leg.qty
        paid = leg.entry_fee + (leg.exit_fee or 0.0)
        return Valued(leg, leg.exit_price, None, None, None, booked, paid)
    q = quote if quote is not None else market.quote(leg.symbol)
    mark = q.mark if q is not None else None
    fees = leg.entry_fee
    gross: float | None = None
    if mark is not None:
        gross = leg.sign * (mark - leg.entry_price) * leg.qty
        fees += market.fee(leg.symbol, mark, leg.qty, buy=leg.side == "sell", at=now)
    return Valued(
        leg,
        mark,
        q.bid if q else None,
        q.ask if q else None,
        q.iv if q else None,
        gross,
        fees,
    )


def held(valued: list[Valued]) -> list[Held]:
    """The open, included legs, as a margin estimate takes them."""
    return [
        Held(
            kind=v.leg.kind,
            side=v.leg.side,
            strike=v.leg.strike,
            expiry=v.leg.expiry,
            delivery=v.leg.delivery,
            qty=v.leg.qty,
            mark=v.mark if v.mark is not None else v.leg.entry_price,
        )
        for v in valued
        if v.leg.open and v.leg.enabled
    ]


def session_net(market: PaperMarket, legs: list[PaperLeg], now: datetime) -> float | None:
    """The included legs' net P&L, or None while an open one has no mark."""
    total = 0.0
    for leg in legs:
        if not leg.enabled:
            continue
        v = value(market, leg, now)
        if v.net is None:
            return None
        total += v.net
    return total


def tick(
    conn: sqlite3.Connection,
    markets: Mapping[str, PaperMarket],
    now: datetime,
    executors: Mapping[str, Executor] | None = None,
) -> list[str]:
    """One pass over every open leg: expiry, each leg's stop and target, then
    each session's whole-position rule. Returns what it did, for the log.

    A live session's stops and rule send real orders through its venue's
    executor; without one - live trading switched off since the legs were
    opened - they are left alone rather than closed on paper while the real
    position stays open."""
    executors = executors or {}
    done: list[str] = []
    sessions: dict[int, PaperSession] = {}
    for leg in paper_repo.open_legs(conn):
        session = sessions.get(leg.session_id) or paper_repo.get_session(conn, leg.session_id)
        if session is None:
            continue
        sessions[session.id] = session
        market = markets.get(session.venue)
        if market is None:
            continue
        try:
            if now >= leg.delivery:
                if settle(conn, market, session, leg, now) is not None:
                    done.append(f"settled {leg.symbol}")
                continue
            if (leg.stop is None and leg.target is None) or not market.is_open(now):
                continue
            executor = executors.get(session.venue) if session.live else None
            if session.live and executor is None:
                continue
            quote = market.quote(leg.symbol)
            if quote is None:
                continue
            reason = triggered(leg, quote)
            if reason is not None:
                exit_leg(conn, market, leg, now, reason, executor)
                done.append(f"{reason} {leg.symbol}")
        except (PaperError, BrokerError) as exc:
            # A thin book or a dropped request: try again next pass.
            log.warning("paper leg %s: %s", leg.id, exc)

    for session in sessions.values():
        if session.rule_stop is None and session.rule_target is None:
            continue
        market = markets.get(session.venue)
        if market is None or not market.is_open(now):
            continue
        rule_executor = executors.get(session.venue) if session.live else None
        if session.live and rule_executor is None:
            continue
        try:
            net = session_net(market, paper_repo.legs(conn, session.id), now)
        except BrokerError as exc:
            log.warning("paper session %s: %s", session.id, exc)
            continue
        if net is None:
            continue
        reason = None
        if session.rule_stop is not None and net <= -session.rule_stop:
            reason = "portfolio stop"
        elif session.rule_target is not None and net >= session.rule_target:
            reason = "portfolio target"
        if reason is not None:
            exit_all(conn, market, session.id, now, reason, rule_executor)
            paper_repo.squared(conn, session.id, now, reason, net)
            done.append(f"{reason} on session {session.id}")
    return done
