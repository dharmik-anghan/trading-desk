"""The simulator: the stored option market at any minute, traded by hand.

    GET  /api/sim/underlyings     every index the simulator can open
    GET  /api/sim/calendar        a month's sessions and expiries, for the date picker
    POST /api/sim/moment          the market at a moment, and the legs marked there
    GET  /api/sim/fetch           how a fetch from Fyers is going
    GET  /api/sim/sessions        saved sessions
    POST /api/sim/sessions        save one (a new one, or overwrite by id)
    DELETE /api/sim/sessions/{id}

The page holds the legs and sends them with every step; the server fills what
has not been priced, applies any stop, target or expiry that the step went
over, and sends them back. See `optbt/simulator.py`.

What the store does not have - an index never fetched, a day before it starts,
an expiry not yet held or one still trading - is fetched from Fyers when a
moment asks for it (`optbt/data/live.py`). The moment says so with `loading`,
and the page asks again once the fetch is done.
"""

from __future__ import annotations

from collections.abc import Hashable
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal, cast

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

import paths
from api.deps import DbPathDep
from api.routers.optbt import STRICT, _history
from api.store import open_db
from broker.factory import expired_source_for, is_configured
from jobs.option_backfill import in_quiet_hours
from optbt.costs import CostModel
from optbt.data.backfill import EARLIEST
from optbt.data.live import Fetcher, Progress
from optbt.data.models import Kind
from optbt.data.source import LiveSource
from optbt.data.store import OptionStore
from optbt.simulator import Clock, LegState, Moment, Rule, SimLeg, moment
from storage import sim_repo
from venues import AssetClass, serving
from venues.calendar import IST
from venues.instruments import OPTION_SERIES

router = APIRouter(tags=["simulator"], prefix="/api/sim")


class SimLegIn(BaseModel):
    model_config = STRICT

    id: str = Field(min_length=1, max_length=64)
    side: Literal["buy", "sell"]
    kind: Literal["CE", "PE"]
    strike: float = Field(gt=0)
    expiry: date
    lots: int = Field(ge=1, le=1000)
    entry_at: datetime
    entry_price: float | None = None
    stop: float | None = Field(default=None, gt=0)
    target: float | None = Field(default=None, gt=0)
    exit_at: datetime | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    enabled: bool = True


class RuleIn(BaseModel):
    """Square everything off when the included legs' net P&L reaches these, in rupees."""

    model_config = STRICT

    stop: float | None = Field(default=None, gt=0)
    target: float | None = Field(default=None, gt=0)


class CostsIn(BaseModel):
    """The backtest's cost model: slippage a fraction of premium per fill, with a
    floor in rupees; brokerage per order. Taxes are the dated statutory ones."""

    model_config = STRICT

    slippage: float = Field(default=0.003, ge=0, le=0.1)
    min_slip: float = Field(default=0.05, ge=0, le=5)
    brokerage: float = Field(default=20.0, ge=0, le=500)


class MomentRequest(BaseModel):
    model_config = STRICT

    underlying: str = "NIFTY"
    #: None opens at the latest session, 09:20.
    at: datetime | None = None
    #: "sod", "eod", "+5m", "-1h", "+1d" ...
    move: str | None = Field(default=None, pattern=r"^(sod|eod|[+-]\d{1,3}[mhd])$")
    #: Which expiry's chain to show; None, the nearest.
    expiry: date | None = None
    #: The moment shown before this one. A step forward from it applies stops,
    #: targets and expiries to every bar in between.
    since: datetime | None = None
    multiplier: int = Field(default=1, ge=1, le=100)
    legs: list[SimLegIn] = Field(default_factory=list, max_length=40)
    rule: RuleIn = Field(default_factory=RuleIn)
    costs: CostsIn = Field(default_factory=CostsIn)


class SideOut(BaseModel):
    ltp: float
    last_at: datetime
    oi: int
    volume: int
    iv: float | None
    delta: float | None


class RowOut(BaseModel):
    strike: float
    ce: SideOut | None
    pe: SideOut | None


class ExpiryOut(BaseModel):
    expiry: date
    days: int
    monthly: bool


class LegOut(SimLegIn):
    status: Literal["pending", "open", "closed", "error"]
    lot_size: int | None
    ltp: float | None
    ltp_at: datetime | None
    iv: float | None
    error: str | None
    #: Charges and slippage, an open leg's exit at its last price included.
    charges: float
    slippage: float


class PayoffOut(BaseModel):
    pnl: float
    realised: float
    #: [spot, P&L] pairs.
    expiry_curve: list[tuple[float, float]]
    today_curve: list[tuple[float, float]]
    max_profit: float | None
    max_loss: float | None
    profit_unlimited: bool
    loss_unlimited: bool
    breakevens: list[float]
    pop: float | None
    sd: list[float]
    #: Margin today's rules would ask for the open legs: SPAN and exposure.
    span: float
    exposure: float
    #: Charges and slippage of the included legs; `net` is `pnl` less them.
    charges: float
    net: float


class FetchOut(BaseModel):
    underlying: str
    day: date
    expiry: date | None
    state: Literal["index", "listing", "fetching", "done", "failed"]
    total: int
    done: int
    bars: int
    failed: int
    error: str | None
    started_at: datetime
    finished_at: datetime | None


class LoadingOut(BaseModel):
    """No moment yet: the day is being fetched. Ask again when it is done."""

    loading: FetchOut


class SquaredOut(BaseModel):
    reason: Literal["portfolio stop", "portfolio target"]
    at: datetime
    net: float


class MomentOut(BaseModel):
    at: datetime
    first: datetime
    last: datetime
    spot: float
    vix: float | None
    future_expiry: date | None
    future: float | None
    expiries: list[ExpiryOut]
    expiry: date | None
    lot_size: int | None
    atm: float | None
    atm_iv: float | None
    rows: list[RowOut]
    legs: list[LegOut]
    payoff: PayoffOut
    #: What is being fetched from Fyers for this moment, if anything.
    loading: FetchOut | None = None
    #: The whole position squared off by its P&L rule on the way here.
    squared: SquaredOut | None = None


def _leg_in(leg: SimLegIn) -> SimLeg:
    return SimLeg(
        id=leg.id,
        side=leg.side,
        kind=Kind(leg.kind),
        strike=leg.strike,
        expiry=leg.expiry,
        lots=leg.lots,
        entry_at=leg.entry_at,
        entry_price=leg.entry_price,
        stop=leg.stop,
        target=leg.target,
        exit_at=leg.exit_at,
        exit_price=leg.exit_price,
        exit_reason=leg.exit_reason,
        enabled=leg.enabled,
    )


def _leg_out(s: LegState) -> LegOut:
    leg = s.leg
    return LegOut(
        id=leg.id,
        side=leg.side,
        kind="CE" if leg.kind is Kind.CALL else "PE",
        strike=leg.strike,
        expiry=leg.expiry,
        lots=leg.lots,
        entry_at=leg.entry_at,
        entry_price=leg.entry_price,
        stop=leg.stop,
        target=leg.target,
        exit_at=leg.exit_at,
        exit_price=leg.exit_price,
        exit_reason=leg.exit_reason,
        enabled=leg.enabled,
        status=s.status,
        lot_size=s.lot_size,
        ltp=s.ltp,
        ltp_at=s.ltp_at,
        iv=s.iv,
        error=s.error,
        charges=s.charges,
        slippage=s.slippage,
    )


def _today() -> date:
    return datetime.now(IST).date()


#: Something to fetch: what it is (so it is asked for once), the day, the expiry.
Want = tuple[Hashable, date, date | None]


def _wants(clock: Clock | None, request: MomentRequest) -> Want | None:
    """A day the request names that the store does not hold: (key, day, None)."""
    today = _today()
    if clock is None:
        day = request.at.date() if request.at else today - timedelta(days=1)
        while day.weekday() >= 5:
            day -= timedelta(days=1)
        return (request.underlying, "day", day), day, None
    if request.at is None or request.move:
        return None
    day = request.at.date()
    if day in clock.days or day.weekday() >= 5 or day >= today or day < EARLIEST:
        return None
    return (request.underlying, "day", day), day, None


def _stale_expiry(m: Moment, underlying: str) -> Want | None:
    """The chosen expiry, if it is not held - or held only as far as some day
    before the moment shows and before yesterday."""
    chosen = next((e for e in m.expiries if e.expiry == m.expiry), None)
    day = m.at.date()
    if chosen is None:
        return (underlying, "expiry-on", day), day, None
    if chosen.status == "missing":
        return (underlying, "expiry", chosen.expiry), day, chosen.expiry
    if chosen.status == "live" and chosen.through is not None:
        wanted = min(day, _today() - timedelta(days=1))
        if chosen.through.date() < wanted:
            return (underlying, "refresh", chosen.expiry, _today()), day, chosen.expiry
    return None


def _fetch(want: Want | None, underlying: str) -> FetchOut | None:
    """Start fetching what is wanted, unless it was asked for already; and say
    what is being fetched now, if anything."""
    fetcher = _live_fetcher()
    if fetcher is None:
        return None
    if want is not None and not fetcher.asked(want[0]):
        fetcher.start(want[0], underlying, want[1], want[2])
    running = fetcher.running
    return _fetch_out(running) if running is not None else None


@router.post("/moment")
def at_moment(request: MomentRequest) -> MomentOut | LoadingOut:
    with _history(request.underlying) as h:
        try:
            clock: Clock | None = Clock(h)
        except ValueError:
            clock = None
        day_wanted = _wants(clock, request)
        if clock is None or day_wanted is not None:
            loading = _fetch(day_wanted, request.underlying)
            if loading is not None:
                return LoadingOut(loading=loading)
            if clock is None:
                raise HTTPException(404, f"No {request.underlying} history to open.")
        m = moment(
            clock,
            request.at,
            request.move,
            request.expiry,
            [_leg_in(leg) for leg in request.legs],
            request.since,
            request.multiplier,
            rule=Rule(request.rule.stop, request.rule.target),
            costs=CostModel(
                brokerage_per_order=request.costs.brokerage,
                slippage=request.costs.slippage,
                min_slip=request.costs.min_slip,
            ),
        )
    p = m.payoff
    return MomentOut(
        at=m.at,
        first=m.first,
        last=m.last,
        spot=m.spot,
        vix=m.vix,
        future_expiry=m.future[0] if m.future else None,
        future=m.future[1] if m.future else None,
        expiries=[ExpiryOut(expiry=e.expiry, days=e.days, monthly=e.monthly) for e in m.expiries],
        expiry=m.expiry,
        lot_size=m.lot_size,
        atm=m.atm,
        atm_iv=m.atm_iv,
        rows=[
            RowOut(
                strike=r.strike,
                ce=SideOut(**vars(r.ce)) if r.ce else None,
                pe=SideOut(**vars(r.pe)) if r.pe else None,
            )
            for r in m.rows
        ],
        legs=[_leg_out(s) for s in m.legs],
        payoff=PayoffOut(**vars(p), net=p.net),
        squared=SquaredOut(**vars(m.squared)) if m.squared else None,
        loading=_fetch(_stale_expiry(m, request.underlying), request.underlying),
    )


# --------------------------------------------------------------------- fetch


_fetcher: Fetcher | None = None


def _live_fetcher() -> Fetcher | None:
    """The fetcher, or None when no broker is set up to fetch from."""
    global _fetcher
    if _fetcher is None:
        venue = serving(AssetClass.INDEX_OPTIONS)
        source = expired_source_for(venue)
        if source is None or not is_configured(venue):
            return None
        _fetcher = Fetcher(
            source=lambda: cast(LiveSource, source()),
            store=lambda: OptionStore(paths.options_store_path()),
            market_open=lambda: in_quiet_hours(datetime.now(UTC)),
            today=_today,
        )
    return _fetcher


def _fetch_out(p: Progress) -> FetchOut:
    return FetchOut(
        underlying=p.underlying,
        day=p.day,
        expiry=p.expiry,
        state=p.state,
        total=p.total,
        done=p.done,
        bars=p.bars,
        failed=len(p.failed),
        error=p.error,
        started_at=p.started_at,
        finished_at=p.finished_at,
    )


@router.get("/fetch")
def fetch_status() -> FetchOut | None:
    """The fetch running, or the last one."""
    p = _fetcher.progress if _fetcher is not None else None
    return _fetch_out(p) if p is not None else None


@router.get("/underlyings")
def underlyings() -> list[str]:
    return list(OPTION_SERIES)


class MonthOut(BaseModel):
    """One month of the calendar, as far as the store knows it."""

    #: Sessions held for the index.
    sessions: list[date]
    #: Option expiries listed.
    expiries: list[date]
    #: The span the store holds sessions for. A weekday inside it that is not a
    #: session was a holiday; outside it, nothing is known yet.
    first: date | None
    last: date | None


@router.get("/calendar")
def calendar(underlying: str = "NIFTY", month: str = Query(pattern=r"^\d{4}-\d{2}$")) -> MonthOut:
    start = date.fromisoformat(f"{month}-01")
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    with _history(underlying) as h:
        held = h.trading_days(date(2000, 1, 1), date(2100, 1, 1))
        return MonthOut(
            sessions=[d for d in held if start <= d <= end],
            expiries=[e for e in h.expiries() if start <= e <= end],
            first=held[0] if held else None,
            last=held[-1] if held else None,
        )


# ------------------------------------------------------------------ sessions


class SessionIn(BaseModel):
    model_config = STRICT

    id: int | None = None
    name: str = Field(min_length=1, max_length=120)
    underlying: str = "NIFTY"
    at: datetime
    #: The page's own state: legs, expiry, multiplier. Opaque here.
    state: dict[str, Any]


class SessionOut(BaseModel):
    id: int
    name: str
    underlying: str
    at: datetime
    state: dict[str, Any]
    saved_at: datetime


@router.get("/sessions")
def sessions(db_path: DbPathDep) -> list[SessionOut]:
    conn = open_db(db_path)
    try:
        return [SessionOut(**vars(s)) for s in sim_repo.listing(conn)]
    finally:
        conn.close()


@router.post("/sessions")
def save_session(body: SessionIn, db_path: DbPathDep) -> SessionOut:
    conn = open_db(db_path)
    try:
        saved = sim_repo.save(conn, body.name, body.underlying, body.at, body.state, body.id)
    except KeyError as exc:
        raise HTTPException(404, f"No saved session {body.id}") from exc
    finally:
        conn.close()
    return SessionOut(**vars(saved))


@router.delete("/sessions/{session_id}")
def delete_session(session_id: int, db_path: DbPathDep) -> dict[str, bool]:
    conn = open_db(db_path)
    try:
        if not sim_repo.delete(conn, session_id):
            raise HTTPException(404, f"No saved session {session_id}")
    finally:
        conn.close()
    return {"deleted": True}
