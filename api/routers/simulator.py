"""The simulator: the stored option market at any minute, traded by hand.

    POST /api/sim/moment          the market at a moment, and the legs marked there
    GET  /api/sim/sessions        saved sessions
    POST /api/sim/sessions        save one (a new one, or overwrite by id)
    DELETE /api/sim/sessions/{id}

The page holds the legs and sends them with every step; the server fills what
has not been priced, applies any stop, target or expiry that the step went
over, and sends them back. See `optbt/simulator.py`.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from api.deps import DbPathDep
from api.routers.optbt import STRICT, _history
from api.store import open_db
from optbt.data.models import Kind
from optbt.simulator import Clock, LegState, SimLeg, moment
from storage import sim_repo

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
    )


@router.post("/moment")
def at_moment(request: MomentRequest) -> MomentOut:
    with _history(request.underlying) as h:
        try:
            clock = Clock(h)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        m = moment(
            clock,
            request.at,
            request.move,
            request.expiry,
            [_leg_in(leg) for leg in request.legs],
            request.since,
            request.multiplier,
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
        payoff=PayoffOut(**vars(p)),
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
