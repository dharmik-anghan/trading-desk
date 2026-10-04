"""The live strategy builder: a strategy put on the live chain, and paper traded.

    GET    /api/live/markets           what can be traded: NSE indices, crypto
    GET    /api/live/state             one expiry's chain now, and a session's legs
    POST   /api/live/resolve           a template or saved strategy, as contracts now
    POST   /api/live/preview           the payoff of a draft before it is traded
    POST   /api/live/orders            a basket, filled now - all of it or none
    GET    /api/live/sessions
    PATCH  /api/live/sessions/{id}     rename, or set the exit-all rule
    DELETE /api/live/sessions/{id}
    POST   /api/live/sessions/{id}/exit-all
    POST   /api/live/legs/{id}/exit
    PATCH  /api/live/legs/{id}         stop, target, and whether it counts
    DELETE /api/live/legs/{id}         a closed leg, out of its session

One page for every market, because a strategy is the same thing on all of them;
what differs - how a fill is found, what it costs, when the market is open - is
`paper/markets.py`'s. The legs are the server's: `jobs/paper_watcher.py` closes
them on a stop, a target, the session's rule or expiry with no page open.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from api.deps import (
    DbPathDep,
    ExecutorsDep,
    LiveLimitsDep,
    PaperMarketsDep,
    SharkAccountDep,
)
from api.routers.optbt import STRICT, LevelIn, StrategyIn
from api.routers.simulator import PayoffOut, SquaredOut
from api.store import open_db
from broker.errors import BrokerError
from broker.shark.options_account import SharkOptionsAccount
from optbt.data.models import Kind
from optbt.engine import Level
from optbt.simulator import LegState, SimLeg, payoff
from optbt.spec import from_dict as spec_from_dict
from paper import desk
from paper import live as live_orders
from paper.desk import PaperError, Valued
from paper.markets import Executor, Held, LiveChain, LiveQuote, PaperMarket, SharkMarket
from paper.resolve import DraftLeg, model_clock, resolve
from storage import paper_repo
from storage.paper_repo import PaperLeg, PaperSession
from venues.calendar import IST

router = APIRouter(tags=["live"], prefix="/api/live")


def _now() -> datetime:
    return datetime.now(UTC)


def _market(markets: dict[str, PaperMarket], underlying: str) -> PaperMarket:
    """The market an underlying trades on."""
    wanted = underlying.upper()
    for market in markets.values():
        try:
            if wanted in market.underlyings():
                return market
        except BrokerError:
            continue
    raise HTTPException(404, f"No live market for {underlying} (is its broker configured?)")


def _fail(exc: Exception) -> HTTPException:
    return HTTPException(409 if isinstance(exc, PaperError) else 502, str(exc))


# -------------------------------------------------------------------- markets


class MarketOut(BaseModel):
    underlying: str
    venue: str
    currency: str
    #: Whether real orders can be sent on it now - SHARK_OPTIONS_LIVE for crypto.
    live: bool
    #: "NSE" or "Crypto": how the picker groups them.
    group: str


@router.get("/markets", response_model=list[MarketOut])
def markets(markets: PaperMarketsDep, executors: ExecutorsDep) -> list[MarketOut]:
    out = []
    for market in markets.values():
        try:
            names = market.underlyings()
        except BrokerError:
            continue
        group = "Crypto" if market.currency == "USDT" else "NSE"
        out += [
            MarketOut(
                underlying=u,
                venue=market.venue,
                currency=market.currency,
                group=group,
                live=market.venue in executors,
            )
            for u in names
        ]
    # The NSE first: it is the desk's home market.
    return sorted(out, key=lambda m: m.group != "NSE")


# ---------------------------------------------------------------------- state


class SideOut(BaseModel):
    """One option of a strike. Shaped as the simulator's chain side, plus the
    book's top and the contract's symbol."""

    ltp: float
    last_at: datetime
    oi: float
    volume: float
    iv: float | None
    delta: float | None
    bid: float | None
    ask: float | None
    mark: float | None
    symbol: str


class RowOut(BaseModel):
    strike: float
    ce: SideOut | None
    pe: SideOut | None


class ExpiryOut(BaseModel):
    expiry: date
    days: int
    monthly: bool
    token: str


class LegOut(BaseModel):
    id: int
    symbol: str
    side: Literal["buy", "sell"]
    kind: Literal["CE", "PE"]
    strike: float
    expiry: date
    #: Units: coins on Shark, contracts (lots x lot size) on the NSE.
    qty: float
    entry_at: datetime
    entry_price: float
    stop: float | None
    target: float | None
    exit_at: datetime | None
    exit_price: float | None
    exit_reason: str | None
    enabled: bool
    status: Literal["open", "closed"]
    mark: float | None
    bid: float | None
    ask: float | None
    iv: float | None
    gross: float | None
    #: Paid, and for an open leg what closing it at the mark would add.
    fees: float
    net: float | None


class SessionOut(BaseModel):
    id: int
    name: str
    #: "live" sessions trade real orders; "paper" ones only here.
    mode: Literal["paper", "live"]
    venue: str
    underlying: str
    created_at: datetime
    rule_stop: float | None
    rule_target: float | None
    squared: SquaredOut | None


class PositionOut(BaseModel):
    symbol: str
    side: Literal["buy", "sell"]
    size: float
    entry: float
    mark: float | None
    unrealised: float | None


class AccountOut(BaseModel):
    """The venue's own view of a live session's account."""

    #: Free in the options wallet, in the quote currency.
    available: float | None
    positions: list[PositionOut]
    #: Where the venue's positions and this session's open legs disagree.
    mismatches: list[str]
    error: str | None = None


class StateOut(BaseModel):
    at: datetime
    underlying: str
    venue: str
    currency: str
    #: Whether orders can be filled now.
    open: bool
    spot: float
    forward: float
    expiries: list[ExpiryOut]
    expiry: date
    expiry_token: str
    atm: float | None
    atm_iv: float | None
    #: Orders move in steps of this many units: one lot, or 0.01 BTC.
    step: float
    min_qty: float
    rows: list[RowOut]
    session: SessionOut | None
    legs: list[LegOut]
    payoff: PayoffOut | None
    #: What the open legs would tie up. An estimate, by each market's own rules.
    margin: float
    #: For a live session: what the venue itself says is held.
    account: AccountOut | None = None


def _session_out(s: PaperSession) -> SessionOut:
    squared = None
    if s.squared_at is not None and s.squared_reason in ("portfolio stop", "portfolio target"):
        squared = SquaredOut(
            reason=s.squared_reason,  # type: ignore[arg-type]
            at=s.squared_at,
            net=s.squared_net or 0.0,
        )
    return SessionOut(
        id=s.id,
        name=s.name,
        mode=s.mode,
        venue=s.venue,
        underlying=s.underlying,
        created_at=s.created_at,
        rule_stop=s.rule_stop,
        rule_target=s.rule_target,
        squared=squared,
    )


def _leg_out(v: Valued) -> LegOut:
    leg = v.leg
    return LegOut(
        id=leg.id,
        symbol=leg.symbol,
        side=leg.side,
        kind=leg.kind,
        strike=leg.strike,
        expiry=leg.expiry,
        qty=leg.qty,
        entry_at=leg.entry_at,
        entry_price=leg.entry_price,
        stop=leg.stop,
        target=leg.target,
        exit_at=leg.exit_at,
        exit_price=leg.exit_price,
        exit_reason=leg.exit_reason,
        enabled=leg.enabled,
        status="open" if leg.open else "closed",
        mark=v.mark,
        bid=v.bid,
        ask=v.ask,
        iv=v.iv,
        gross=v.gross,
        fees=v.fees,
        net=v.net,
    )


def _side(symbol: str, q: LiveQuote, at: datetime) -> SideOut:
    return SideOut(
        ltp=q.ltp or q.mark or 0.0,
        last_at=at,
        oi=q.oi,
        volume=q.volume,
        iv=q.iv,
        delta=q.delta,
        bid=q.bid,
        ask=q.ask,
        mark=q.mark,
        symbol=symbol,
    )


def _atm(chain: LiveChain) -> tuple[float | None, float | None]:
    both = [r for r in chain.rows if r.ce and r.pe]
    if not both:
        return None, None
    row = min(both, key=lambda r: abs(r.strike - chain.reference))
    ivs = [s[1].iv for s in (row.ce, row.pe) if s is not None and s[1].iv]
    return row.strike, (sum(ivs) / len(ivs) if ivs else None)


def _quotes(chain: LiveChain) -> dict[str, LiveQuote]:
    out = {}
    for r in chain.rows:
        for side in (r.ce, r.pe):
            if side is not None:
                out[side[0]] = side[1]
    return out


def _payoff(
    rows: Sequence[tuple[SimLeg, str, float, float | None, float | None, float, datetime]],
    spot: float,
    atm_iv: float | None,
    now: datetime,
) -> PayoffOut | None:
    """The simulator's payoff over legs: (leg, status, qty, mark, iv, fees, delivery)."""
    if not rows:
        return None
    states = [
        LegState(
            sim,
            status,  # type: ignore[arg-type]
            # One "lot" of the leg's own size: the payoff multiplies lots by lot
            # size, and a coin's quantity is fractional.
            lot_size=qty,  # type: ignore[arg-type]
            ltp=mark,
            iv=iv,
            charges=fees,
        )
        for sim, status, qty, mark, iv, fees, _ in rows
    ]
    delivery = min(d for *_, d in rows)
    p = payoff(states, model_clock(now, delivery), spot, atm_iv, 1)
    # The NSE's SPAN is not every market's; the state's own margin is reported.
    return PayoffOut(**vars(p), net=p.net).model_copy(update={"span": 0.0, "exposure": 0.0})


def _sim(leg: PaperLeg) -> SimLeg:
    return SimLeg(
        id=str(leg.id),
        side=leg.side,
        kind=Kind.CALL if leg.kind == "CE" else Kind.PUT,
        strike=leg.strike,
        expiry=leg.expiry,
        lots=1,
        entry_at=leg.entry_at,
        entry_price=leg.entry_price,
        stop=leg.stop,
        target=leg.target,
        exit_at=leg.exit_at,
        exit_price=leg.exit_price,
        exit_reason=leg.exit_reason,
        enabled=leg.enabled,
    )


@router.get("/state", response_model=StateOut)
def state(
    markets: PaperMarketsDep,
    db_path: DbPathDep,
    account: SharkAccountDep,
    underlying: str = "NIFTY",
    expiry: str = "",
    strikes: int = Query(default=15, ge=1, le=60),
    session_id: int | None = None,
) -> StateOut:
    market = _market(markets, underlying)
    now = _now()
    try:
        chain = market.chain(underlying.upper(), expiry, strikes)
    except (BrokerError, PaperError) as exc:
        raise _fail(exc) from exc
    atm, atm_iv = _atm(chain)
    quotes = _quotes(chain)

    session = None
    valued: list[Valued] = []
    if session_id is not None:
        conn = open_db(db_path)
        try:
            found = paper_repo.get_session(conn, session_id)
            if found is None:
                raise HTTPException(404, f"No paper session {session_id}")
            session = _session_out(found)
            legs = paper_repo.legs(conn, session_id)
        finally:
            conn.close()
        try:
            valued = [desk.value(market, leg, now, quotes.get(leg.symbol)) for leg in legs]
        except BrokerError as exc:
            raise _fail(exc) from exc

    payoff_out = _payoff(
        [
            (
                _sim(v.leg),
                "open" if v.leg.open else "closed",
                v.leg.qty,
                v.mark if v.leg.open else v.leg.exit_price,
                v.iv,
                v.fees,
                v.leg.delivery,
            )
            for v in valued
        ],
        chain.spot,
        atm_iv,
        now,
    )
    try:
        margin = market.margin(desk.held(valued), chain.underlying, now) if valued else 0.0
    except BrokerError:
        margin = 0.0
    return StateOut(
        at=now,
        underlying=chain.underlying,
        venue=chain.venue,
        currency=chain.currency,
        open=chain.open,
        spot=chain.spot,
        forward=chain.forward,
        expiries=[
            ExpiryOut(
                expiry=e.expiry,
                days=(e.expiry - now.date()).days,
                monthly=e.monthly,
                token=e.token,
            )
            for e in chain.expiries
        ],
        expiry=chain.expiry.expiry,
        expiry_token=chain.expiry.token,
        atm=atm,
        atm_iv=atm_iv,
        step=chain.step,
        min_qty=chain.min_qty,
        rows=[
            RowOut(
                strike=r.strike,
                ce=_side(*r.ce, chain.fetched_at) if r.ce else None,
                pe=_side(*r.pe, chain.fetched_at) if r.pe else None,
            )
            for r in chain.rows
        ],
        session=session,
        legs=[_leg_out(v) for v in valued],
        payoff=payoff_out,
        margin=margin,
        account=(
            _account(account, markets, valued)
            if session is not None and session.mode == "live"
            else None
        ),
    )


def _account(
    account: SharkOptionsAccount | None, markets: dict[str, PaperMarket], valued: list[Valued]
) -> AccountOut:
    """Shark's positions beside the session's open legs, and where they differ.

    Net per contract on both sides: the venue nets a contract into one position,
    and this session may hold it as several legs - or share it with another live
    session, which the message says rather than guessing which is wrong."""
    if account is None:
        return AccountOut(available=None, positions=[], mismatches=[], error="no Shark key")
    try:
        held = account.positions()
        shark = markets.get("shark_options")
        available = (
            live_orders.wallet_usdt(shark.broker, account)
            if isinstance(shark, SharkMarket)
            else None
        )
    except BrokerError as exc:
        return AccountOut(available=None, positions=[], mismatches=[], error=str(exc))
    venue = {p.symbol: (1 if p.side == "buy" else -1) * p.size for p in held}
    ours: dict[str, float] = {}
    for v in valued:
        if v.leg.open:
            ours[v.leg.symbol] = ours.get(v.leg.symbol, 0.0) + v.leg.sign * v.leg.qty
    mismatches = [
        f"{sym}: Shark holds {venue.get(sym, 0.0):+g}, this session {ours.get(sym, 0.0):+g}"
        for sym in sorted(set(venue) | set(ours))
        if abs(venue.get(sym, 0.0) - ours.get(sym, 0.0)) > 1e-9
    ]
    return AccountOut(
        available=available,
        positions=[PositionOut(**vars(p)) for p in held],
        mismatches=mismatches,
    )


# --------------------------------------------------------------------- drafts


class LevelOut(BaseModel):
    kind: Literal["pct", "points"]
    value: float


class DraftOut(BaseModel):
    side: Literal["buy", "sell"]
    kind: Literal["CE", "PE"]
    qty: float
    symbol: str | None
    strike: float | None
    expiry: date | None
    expiry_token: str | None
    bid: float | None
    ask: float | None
    mark: float | None
    stop: LevelOut | None
    target: LevelOut | None
    error: str | None


class RuleOut(BaseModel):
    """A whole-position exit a strategy carries: in money, or as a share of the
    credit taken in, which only a fill can turn into money."""

    mtm_stop: float | None = None
    mtm_target: float | None = None
    stop_credit: float | None = None
    target_credit: float | None = None


class ResolveIn(BaseModel):
    model_config = STRICT

    underlying: str
    spec: StrategyIn


class ResolveOut(BaseModel):
    legs: list[DraftOut]
    rule: RuleOut


def _level_out(level: Level | None) -> LevelOut | None:
    if level is None:
        return None
    return LevelOut(kind=level.kind, value=level.value)


def _draft_out(d: DraftLeg) -> DraftOut:
    return DraftOut(
        side=d.side,
        kind=d.kind,
        qty=d.qty,
        symbol=d.symbol,
        strike=d.strike,
        expiry=d.expiry,
        expiry_token=d.expiry_token,
        bid=d.bid,
        ask=d.ask,
        mark=d.mark,
        stop=_level_out(d.stop),
        target=_level_out(d.target),
        error=d.error,
    )


@router.post("/resolve", response_model=ResolveOut)
def resolve_strategy(body: ResolveIn, markets: PaperMarketsDep) -> ResolveOut:
    """The spec's legs as contracts on the chain now. Nothing is traded."""
    market = _market(markets, body.underlying)
    config = spec_from_dict(body.spec.model_dump(mode="json"))
    name = body.underlying.upper()
    now = _now()
    try:
        first = market.chain(name, "", 40)
        legs = resolve(
            config,
            {first.expiry.token: first},
            first.expiries,
            now,
            # Crypto lists dailies between Friday weeklies; on the NSE every
            # listed expiry is a weekly.
            fridays_weekly=market.currency == "USDT",
            load=lambda token: market.chain(name, token, 40),
        )
    except (BrokerError, PaperError) as exc:
        raise _fail(exc) from exc
    return ResolveOut(
        legs=[_draft_out(d) for d in legs],
        rule=RuleOut(
            mtm_stop=config.mtm_stop,
            mtm_target=config.mtm_target,
            stop_credit=config.stop_credit,
            target_credit=config.target_credit,
        ),
    )


class DraftIn(BaseModel):
    model_config = STRICT

    symbol: str = Field(min_length=1, max_length=64)
    side: Literal["buy", "sell"]
    #: Units: coins, or contracts (lots x lot size).
    qty: float = Field(gt=0)
    stop: LevelIn | None = None
    target: LevelIn | None = None


class PreviewIn(BaseModel):
    model_config = STRICT

    underlying: str
    legs: list[DraftIn] = Field(min_length=1, max_length=12)


class PreviewOut(BaseModel):
    payoff: PayoffOut | None
    margin: float
    #: What filling it now would take in (+) or pay out (-), before fees.
    premium: float
    fees: float
    #: Why a leg would not fill now, by symbol.
    problems: dict[str, str]


@router.post("/preview", response_model=PreviewOut)
def preview(body: PreviewIn, markets: PaperMarketsDep) -> PreviewOut:
    """The draft as if filled now: at the ask to buy and the bid to sell."""
    market = _market(markets, body.underlying)
    now = _now()
    rows = []
    held = []
    premium = fees = 0.0
    problems: dict[str, str] = {}
    spot = market.spot(body.underlying.upper())
    for i, d in enumerate(body.legs):
        try:
            c = market.contract(d.symbol)
            if c is None:
                raise PaperError("not listed")
            price = market.fill(d.symbol, d.side, d.qty)
            fee = market.fee(d.symbol, price, d.qty, buy=d.side == "buy", at=now)
            q = market.quote(d.symbol)
        except (PaperError, BrokerError) as exc:
            problems[d.symbol] = str(exc)
            continue
        premium += (price if d.side == "sell" else -price) * d.qty
        fees += fee
        sim = SimLeg(
            id=str(i),
            side=d.side,
            kind=Kind.CALL if c.kind == "CE" else Kind.PUT,
            strike=c.strike,
            expiry=c.expiry,
            lots=1,
            entry_at=now,
            entry_price=price,
        )
        mark = q.mark if q is not None and q.mark is not None else price
        exit_fee = market.fee(d.symbol, mark, d.qty, buy=d.side == "sell", at=now)
        rows.append((sim, "open", d.qty, mark, q.iv if q else None, fee + exit_fee, c.delivery))
        held.append(
            Held(
                kind=c.kind,
                side=d.side,
                strike=c.strike,
                expiry=c.expiry,
                delivery=c.delivery,
                qty=d.qty,
                mark=mark,
            )
        )
    try:
        margin = market.margin(held, body.underlying.upper(), now)
    except BrokerError:
        margin = 0.0
    return PreviewOut(
        payoff=_payoff(rows, spot or 0.0, None, now) if spot else None,
        margin=margin,
        premium=premium,
        fees=fees,
        problems=problems,
    )


# --------------------------------------------------------------------- orders


class OrdersIn(BaseModel):
    model_config = STRICT

    #: The session to add to; absent, a new one is started and named `name`.
    session_id: int | None = None
    name: str | None = Field(default=None, max_length=80)
    underlying: str
    legs: list[DraftIn] = Field(min_length=1, max_length=12)
    rule: RuleOut | None = None
    #: "live" sends real orders. A new session takes this mode; an existing one
    #: must already be in it.
    mode: Literal["paper", "live"] = "paper"
    #: Must be true for a live basket: the page asks first, and a script that
    #: did not mean to trade for real is refused here.
    confirm: bool = False


class OrdersOut(BaseModel):
    session: SessionOut
    legs: list[LegOut]
    #: A live basket that stopped part-way: what happened, and what is left open.
    problem: str | None = None


def _level_price(level: LevelIn | None, entry: float, side: str, *, stop: bool) -> float | None:
    """A level from the fill: a stop against the position, a target for it."""
    if level is None:
        return None
    move = entry * level.value if level.kind == "pct" else level.value
    against = 1 if side == "sell" else -1
    price = entry + (against if stop else -against) * move
    return price if price > 0 else None


@router.post("/orders", response_model=OrdersOut)
def orders(
    body: OrdersIn,
    markets: PaperMarketsDep,
    executors: ExecutorsDep,
    limits: LiveLimitsDep,
    account: SharkAccountDep,
    db_path: DbPathDep,
) -> OrdersOut:
    market = _market(markets, body.underlying)
    now = _now()
    name = body.underlying.upper()
    executor = None
    if body.mode == "live":
        executor = executors.get(market.venue)
        if executor is None:
            raise HTTPException(409, f"Live trading is not available on {name}.")
        if not body.confirm:
            raise HTTPException(409, "A live order needs confirm: true.")
    conn = open_db(db_path)
    try:
        session: PaperSession | None = None
        if body.session_id is not None:
            session = paper_repo.get_session(conn, body.session_id)
            if session is None:
                raise HTTPException(404, f"No paper session {body.session_id}")
            if session.underlying != name:
                raise HTTPException(409, f"That session trades {session.underlying}, not {name}.")
            if session.mode != body.mode:
                raise HTTPException(409, f"That session is {session.mode}, not {body.mode}.")
        # Every order priced before anything is booked - or a session started -
        # so a basket the market refuses leaves nothing half-built behind it.
        target_session = session or PaperSession(
            0, "", market.venue, name, now, None, None, None, None, None, body.mode
        )
        try:
            priced = [
                desk.price_order(market, target_session, d.symbol, d.side, d.qty, now)
                for d in body.legs
            ]
            if executor is not None:
                live_orders.check(
                    conn,
                    market,
                    limits,
                    priced,
                    name,
                    now,
                    available=(
                        (lambda: live_orders.wallet_usdt(market.broker, account))
                        if account is not None and isinstance(market, SharkMarket)
                        else None
                    ),
                )
        except (PaperError, BrokerError) as exc:
            raise _fail(exc) from exc
        if session is None:
            stamp = now.astimezone(IST).strftime("%d %b %H:%M")
            prefix = "LIVE " if body.mode == "live" else ""
            label = (body.name or "").strip() or f"{prefix}{name} {stamp}"
            session = paper_repo.create_session(conn, label, market.venue, name, body.mode)
        problem = None
        if executor is not None:
            legs, problem = desk.open_live(conn, market, executor, session, priced, now)
        else:
            legs = [desk.book(conn, market, session, p, now) for p in priced]
        for leg, d in zip(legs, body.legs, strict=True):
            stop = _level_price(d.stop, leg.entry_price, leg.side, stop=True)
            target = _level_price(d.target, leg.entry_price, leg.side, stop=False)
            if stop is not None or target is not None:
                paper_repo.set_levels(conn, leg.id, stop=stop, target=target, enabled=True)
        if body.rule is not None:
            credit = sum((1 if g.side == "sell" else -1) * g.entry_price * g.qty for g in legs)
            rule_stop = body.rule.mtm_stop
            rule_target = body.rule.mtm_target
            # A share of the credit means nothing for a debit, which took none.
            if credit > 0:
                if body.rule.stop_credit is not None:
                    rule_stop = credit * body.rule.stop_credit
                if body.rule.target_credit is not None:
                    rule_target = credit * body.rule.target_credit
            if rule_stop is not None or rule_target is not None:
                paper_repo.set_rule(conn, session.id, rule_stop, rule_target)
        session = paper_repo.get_session(conn, session.id)
        booked = [found for g in legs if (found := paper_repo.get_leg(conn, g.id)) is not None]
    finally:
        conn.close()
    assert session is not None
    return OrdersOut(
        session=_session_out(session),
        legs=[_leg_out(desk.value(market, g, now)) for g in booked],
        problem=problem,
    )


# ------------------------------------------------------------------- sessions


@router.get("/sessions", response_model=list[SessionOut])
def sessions(db_path: DbPathDep, underlying: str | None = None) -> list[SessionOut]:
    conn = open_db(db_path)
    try:
        found = paper_repo.sessions(conn, underlying.upper() if underlying else None)
    finally:
        conn.close()
    return [_session_out(s) for s in found]


class SessionPatch(BaseModel):
    model_config = STRICT

    name: str | None = Field(default=None, min_length=1, max_length=80)
    #: Square everything off at this net P&L, in the market's money. Sent as
    #: null to clear; left out, unchanged.
    rule_stop: float | None = Field(default=None, gt=0)
    rule_target: float | None = Field(default=None, gt=0)


@router.patch("/sessions/{session_id}", response_model=SessionOut)
def patch_session(session_id: int, body: SessionPatch, db_path: DbPathDep) -> SessionOut:
    conn = open_db(db_path)
    try:
        s = paper_repo.get_session(conn, session_id)
        if s is None:
            raise HTTPException(404, f"No paper session {session_id}")
        if body.name is not None:
            paper_repo.rename_session(conn, session_id, body.name.strip())
        sent = body.model_fields_set
        if "rule_stop" in sent or "rule_target" in sent:
            paper_repo.set_rule(
                conn,
                session_id,
                body.rule_stop if "rule_stop" in sent else s.rule_stop,
                body.rule_target if "rule_target" in sent else s.rule_target,
            )
        updated = paper_repo.get_session(conn, session_id)
    finally:
        conn.close()
    assert updated is not None
    return _session_out(updated)


@router.delete("/sessions/{session_id}")
def delete_session(session_id: int, db_path: DbPathDep) -> dict[str, bool]:
    conn = open_db(db_path)
    try:
        if any(leg.open for leg in paper_repo.legs(conn, session_id)):
            raise HTTPException(409, "Exit the open legs before deleting the session.")
        if not paper_repo.delete_session(conn, session_id):
            raise HTTPException(404, f"No paper session {session_id}")
    finally:
        conn.close()
    return {"deleted": True}


def _session_market(
    conn: sqlite3.Connection, markets: dict[str, PaperMarket], session_id: int
) -> PaperMarket:
    s = paper_repo.get_session(conn, session_id)
    if s is None:
        raise HTTPException(404, f"No paper session {session_id}")
    market = markets.get(s.venue)
    if market is None:
        raise HTTPException(503, f"{s.venue} is not configured")
    return market


def _session_executor(
    conn: sqlite3.Connection, executors: dict[str, Executor], session_id: int, confirm: bool
) -> Executor | None:
    """A live session's executor - refused without `confirm`, or with live
    trading off, since closing a real position on paper would leave it open."""
    s = paper_repo.get_session(conn, session_id)
    if s is None or not s.live:
        return None
    executor = executors.get(s.venue)
    if executor is None:
        raise HTTPException(
            409, "Live trading is off, so this real position cannot be closed from here."
        )
    if not confirm:
        raise HTTPException(409, "Closing a live position needs confirm=true.")
    return executor


@router.post("/sessions/{session_id}/exit-all")
def exit_all(
    session_id: int,
    markets: PaperMarketsDep,
    executors: ExecutorsDep,
    db_path: DbPathDep,
    confirm: bool = False,
) -> dict[str, list[str]]:
    conn = open_db(db_path)
    try:
        market = _session_market(conn, markets, session_id)
        executor = _session_executor(conn, executors, session_id, confirm)
        problems = desk.exit_all(conn, market, session_id, _now(), executor=executor)
    finally:
        conn.close()
    return {"problems": problems}


# ----------------------------------------------------------------------- legs


def _leg(conn: sqlite3.Connection, leg_id: int) -> PaperLeg:
    found = paper_repo.get_leg(conn, leg_id)
    if found is None:
        raise HTTPException(404, f"No paper leg {leg_id}")
    return found


@router.post("/legs/{leg_id}/exit", response_model=LegOut)
def exit_leg(
    leg_id: int,
    markets: PaperMarketsDep,
    executors: ExecutorsDep,
    db_path: DbPathDep,
    confirm: bool = False,
) -> LegOut:
    now = _now()
    conn = open_db(db_path)
    try:
        leg = _leg(conn, leg_id)
        market = _session_market(conn, markets, leg.session_id)
        executor = _session_executor(conn, executors, leg.session_id, confirm)
        try:
            closed = desk.exit_leg(conn, market, leg, now, executor=executor)
        except (PaperError, BrokerError) as exc:
            raise _fail(exc) from exc
    finally:
        conn.close()
    return _leg_out(desk.value(market, closed, now))


class LegPatch(BaseModel):
    model_config = STRICT

    #: Option prices; null clears. Left out, unchanged.
    stop: float | None = Field(default=None, gt=0)
    target: float | None = Field(default=None, gt=0)
    enabled: bool | None = None


@router.patch("/legs/{leg_id}", response_model=LegOut)
def patch_leg(leg_id: int, body: LegPatch, markets: PaperMarketsDep, db_path: DbPathDep) -> LegOut:
    now = _now()
    conn = open_db(db_path)
    try:
        leg = _leg(conn, leg_id)
        market = _session_market(conn, markets, leg.session_id)
        sent = body.model_fields_set
        stop = body.stop if "stop" in sent else leg.stop
        target = body.target if "target" in sent else leg.target
        if leg.side == "sell":
            # A short's stop is above where it sold, its target below.
            if stop is not None and stop <= leg.entry_price:
                raise HTTPException(422, "A short's stop must be above its entry.")
            if target is not None and target >= leg.entry_price:
                raise HTTPException(422, "A short's target must be below its entry.")
        else:
            if stop is not None and stop >= leg.entry_price:
                raise HTTPException(422, "A long's stop must be below its entry.")
            if target is not None and target <= leg.entry_price:
                raise HTTPException(422, "A long's target must be above its entry.")
        enabled = body.enabled if body.enabled is not None else leg.enabled
        paper_repo.set_levels(conn, leg_id, stop=stop, target=target, enabled=enabled)
        updated = _leg(conn, leg_id)
    finally:
        conn.close()
    return _leg_out(desk.value(market, updated, now))


@router.delete("/legs/{leg_id}")
def delete_leg(leg_id: int, db_path: DbPathDep) -> dict[str, bool]:
    conn = open_db(db_path)
    try:
        leg = _leg(conn, leg_id)
        if leg.open:
            raise HTTPException(409, "Exit the leg before removing it.")
        paper_repo.delete_leg(conn, leg_id)
    finally:
        conn.close()
    return {"deleted": True}
