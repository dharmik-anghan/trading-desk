"""Options backtesting: running a strategy over the stored NIFTY chain.

A sibling of `backtest.py`, as `optbt` is of `backtest`. Three calls:

    GET  /api/optbt/coverage   what the store holds, so the page offers only real dates
    POST /api/optbt/run        a strategy's config and a window -> the result, in full
    POST /api/optbt/replay     the minute bars behind one trade, to draw it

The store is opened per request, read-only, and closed again. DuckDB will not let
a reader in while another process writes, and the backfill writes for hours, so a
held connection would either block the backfill or be refused by it. When it is
refused, the answer is a 503 that says why rather than an opaque failure.

`OPTBT_STORE` points the desk at a different file - a copy taken while a backfill
runs, for instance.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, time
from pathlib import Path
from typing import Literal

import duckdb
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

import paths
from optbt.costs import CostModel
from optbt.data.history import History
from optbt.data.models import Kind
from optbt.engine import Engine, Leg, Result, Trade
from optbt.market import OptionKey
from optbt.results import summarise
from optbt.spec import from_dict as spec_from_dict
from optbt.strategies.legs import (
    LegsConfig,
    LegStrategy,
)
from venues.instruments import OPTION_SERIES

router = APIRouter(tags=["optbt"], prefix="/api/optbt")

#: Points on the returned equity curve are days, so four years is ~1,000 - small
#: enough to send whole, which keeps the drawdown on the page the true one.


def _store_path() -> Path:
    return paths.options_store_path()


#: Requests refuse fields they do not know. A frontend newer than the backend it
#: talks to used to send conditions the backend silently dropped, and every run
#: came back identical - which looked like the page not updating.
STRICT = ConfigDict(extra="forbid")


@contextmanager
def _history(underlying: str = "NIFTY") -> Iterator[History]:
    if underlying not in OPTION_SERIES:
        raise HTTPException(422, f"No such underlying: {underlying}")
    path = _store_path()
    if not path.exists():
        raise HTTPException(
            404, f"No option store at {path}. Run scripts/backfill_options.py to fetch one."
        )
    try:
        history = History.open(path, underlying)
    except duckdb.IOException as exc:
        raise HTTPException(
            503,
            "The option store stayed locked by another process for 30 seconds. A "
            "backfill only holds it while writing, so this is most likely an older "
            "backfill still running from before that change - stop it and run again. "
            f"({exc})",
        ) from exc
    try:
        yield history
    finally:
        history.close()


# ------------------------------------------------------------------ coverage


class Coverage(BaseModel):
    store: str
    underlying: str
    first_day: date | None
    last_day: date | None
    #: Settled expiries the calendar lists, and how many of them are held.
    expiries_listed: int
    expiries_held: int
    first_expiry: date | None
    last_expiry: date | None
    contracts: int
    bars: int


def _coverage(h: History) -> Coverage:
    return Coverage(store=str(_store_path()), underlying=h.underlying, **vars(h.coverage()))


@router.get("/coverage")
def coverage(underlying: str = "NIFTY") -> Coverage:
    with _history(underlying) as h:
        return _coverage(h)


@router.get("/underlyings", response_model=list[Coverage])
def underlyings() -> list[Coverage]:
    """Every underlying the store holds options for, with its tradable window.

    What the page offers to pick from - so adding BANKNIFTY is a backfill, not
    a change to the page.
    """
    with _history() as h:
        names = h.underlyings()
    out = []
    for name in names:
        if name in OPTION_SERIES:
            with _history(name) as h:
                out.append(_coverage(h))
    return out


# ----------------------------------------------------------------------- run


class LevelIn(BaseModel):
    """A distance from a leg's fill. `pct` as a fraction: 0.25 is 25%."""

    model_config = STRICT

    kind: Literal["pct", "points"]
    value: float = Field(gt=0, le=10_000)


class StrikeIn(BaseModel):
    model_config = STRICT

    #: "atm": `offset` listed strikes from the money, + OTM, - ITM.
    #: "premium": the strike whose premium is closest to `premium`.
    #: "straddle_width": `width_mult` times the ATM straddle's premium, away
    #: from the ATM strike. "sp_pct": the strike whose own premium is closest
    #: to `sp_pct` percent of the ATM straddle's premium.
    mode: Literal["atm", "premium", "pct", "delta", "straddle_width", "sp_pct"] = "atm"
    offset: int = Field(default=0, ge=-20, le=20)
    premium: float = Field(default=0.0, ge=0)
    pct: float = Field(default=0.0, ge=-30, le=30)
    delta: float = Field(default=0.30, gt=0, lt=1)
    width_mult: float = Field(default=1.0, gt=0, le=5)
    sp_pct: float = Field(default=25.0, gt=0, le=200)


class ExpiryIn(BaseModel):
    """Which expiry: see optbt.strategies.legs.ExpiryChoice."""

    model_config = STRICT

    series: Literal["daily", "weekly", "monthly", "days"] = "weekly"
    nth: int = Field(default=1, ge=1, le=3)
    #: Trading sessions an expiry must have left to be taken; 1 skips it on its own day.
    min_left: int = Field(default=0, ge=0, le=10)
    days: int = Field(default=45, ge=1, le=120)


class LegIn(BaseModel):
    model_config = STRICT

    side: Literal["buy", "sell"]
    kind: Literal["CE", "PE"]
    lots: int = Field(default=1, ge=1, le=100)
    #: This leg's own expiry; none trades the strategy's. The first version of
    #: the request named it - "week", "next_week", "month", "next_month", "days" -
    #: with `expiry_days` beside it, and a page or script still sending that is
    #: read as the choice it meant rather than refused.
    expiry: ExpiryIn | Literal["week", "next_week", "month", "next_month", "days"] | None = None
    expiry_days: int | None = Field(default=None, ge=1, le=120)
    strike: StrikeIn = Field(default_factory=StrikeIn)
    stop: LevelIn | None = None
    target: LevelIn | None = None


class DaysIn(BaseModel):
    """Which days to trade. Every bound is optional; see optbt.strategies.legs.DayFilter."""

    model_config = STRICT

    expiry_day: Literal["any", "only", "skip", "skip_eve"] = "any"
    dte_min: int | None = Field(default=None, ge=0)
    dte_max: int | None = Field(default=None, ge=0)
    vix_min: float | None = None
    vix_max: float | None = None
    vix_pct_min: float | None = Field(default=None, ge=0, le=100)
    vix_pct_max: float | None = Field(default=None, ge=0, le=100)
    vix_lookback: int = Field(default=252, ge=20, le=2000)
    gap_min: float | None = None
    gap_max: float | None = None
    open_zones: list[str] = Field(default_factory=list)


class AdjustIn(BaseModel):
    """Move the untested side in when spot reaches a wing. See
    optbt.strategies.legs.Adjustment - distances are in index points."""

    model_config = STRICT

    enabled: bool = False
    near_points: float = Field(default=50, ge=0, le=2000)
    fall_from: Literal["long", "short"] = "long"
    fall_points: float = Field(default=200, ge=-2000, le=2000)
    rise_from: Literal["long", "short"] = "short"
    rise_points: float = Field(default=0, ge=-2000, le=2000)
    move_wing: bool = True
    max_per_trade: int = Field(default=1, ge=1, le=5)


class TriggerIn(BaseModel):
    """When the entry fires. See optbt.strategies.legs.EntryTrigger."""

    model_config = STRICT

    mode: Literal["time", "move_pct", "range_breakout"] = "time"
    move_pct: float = Field(default=0.5, gt=0, le=20)
    range_until: time | None = None


class ReEntryIn(BaseModel):
    """Trying the same legs again after the position goes flat, same day.
    See optbt.strategies.legs.ReEntry."""

    model_config = STRICT

    enabled: bool = False
    trigger: Literal["leg_stop", "mtm_stop", "any"] = "leg_stop"
    max_times: int = Field(default=1, ge=1, le=10)


class OperandIn(BaseModel):
    """One side of an indicator condition. See optbt.signals.Operand."""

    model_config = STRICT

    kind: Literal["price", "ema", "sma", "rsi", "supertrend", "level", "number"] = "price"
    length: int = Field(default=20, ge=1, le=500)
    mult: float = Field(default=3.0, gt=0, le=20)
    level: Literal["P", "R1", "R2", "R3", "S1", "S2", "S3", "PDH", "PDL", "PDC", "DO"] = "P"
    value: float = 0.0


class ConditionIn(BaseModel):
    model_config = STRICT

    left: OperandIn
    op: Literal["above", "below", "crosses_above", "crosses_below"]
    right: OperandIn
    #: Minutes per candle.
    timeframe: Literal[1, 3, 5, 10, 15, 30, 60] = 5


class EntrySignalIn(BaseModel):
    """Indicator conditions on the entry. See optbt.signals.EntrySignal."""

    model_config = STRICT

    mode: Literal["take_if", "skip_if", "wait"] = "take_if"
    join: Literal["all", "any"] = "all"
    conditions: list[ConditionIn] = Field(default_factory=list, max_length=6)


class ExitSignalIn(BaseModel):
    model_config = STRICT

    join: Literal["all", "any"] = "any"
    conditions: list[ConditionIn] = Field(default_factory=list, max_length=6)


class StrategyIn(BaseModel):
    """A strategy as legs, and everything that decides what running it means -
    but not where or over what window. What a saved strategy holds."""

    model_config = STRICT

    legs: list[LegIn] = Field(min_length=1, max_length=8)
    expiry: ExpiryIn = Field(default_factory=ExpiryIn)
    entry: time = time(9, 20)
    exit: time = time(15, 15)
    #: Monday is 0.
    weekdays: list[int] = Field(default=[0, 1, 2, 3, 4])
    hold: Literal["intraday", "expiry"] = "intraday"
    mtm_stop: float | None = Field(default=None, gt=0)
    mtm_target: float | None = Field(default=None, gt=0)
    #: Fractions of the credit taken in: 0.5 is half of it.
    target_credit: float | None = Field(default=None, gt=0, le=10)
    stop_credit: float | None = Field(default=None, gt=0, le=10)
    exit_dte: int | None = Field(default=None, ge=0, le=120)
    trail_to_cost: bool = False
    days: DaysIn = Field(default_factory=DaysIn)
    adjust: AdjustIn = Field(default_factory=AdjustIn)
    equal_wings: bool = False
    trigger: TriggerIn = Field(default_factory=TriggerIn)
    reentry: ReEntryIn = Field(default_factory=ReEntryIn)
    entry_signal: EntrySignalIn = Field(default_factory=EntrySignalIn)
    exit_signal: ExitSignalIn = Field(default_factory=ExitSignalIn)
    #: Slippage per fill as a fraction of premium, and its floor in rupees.
    slippage: float = Field(default=0.003, ge=0, le=0.1)
    min_slip: float = Field(default=0.05, ge=0, le=5)
    brokerage: float = Field(default=20.0, ge=0, le=500)

    def config(self) -> LegsConfig:
        """The strategy part of the request. Its JSON is the spec's shape - see optbt.spec."""
        return spec_from_dict(self.model_dump(mode="json"))


class RunRequest(StrategyIn):
    """A strategy, and the underlying and window to run it over."""

    underlying: str = "NIFTY"
    start: date
    end: date


class LegOut(BaseModel):
    tag: str
    expiry: date
    strike: float
    kind: str
    side: Literal["buy", "sell"]
    lots: int
    lot_size: int
    entry_at: datetime
    entry: float
    stop: float | None
    exit_at: datetime | None
    exit: float | None
    ended: str | None
    pnl: float
    charges: float


class TradeOut(BaseModel):
    id: int
    opened: datetime
    closed: datetime | None
    ended: str
    gross: float
    charges: float
    net: float
    legs: list[LegOut]
    events: list[str]
    #: The day at entry: weekday, dte, sessions_to_expiry, expiry_day, vix, vix_pct, gap_pct,
    #: open_zone, month, spot. What the result explorer slices by.
    tags: dict[str, str | float | int | bool | None]
    #: Lowest and highest gross P&L at any minute's close while open.
    worst: float
    best: float


class ChargesOut(BaseModel):
    brokerage: float
    stt: float
    exchange: float
    sebi: float
    stamp: float
    gst: float
    total: float


class SummaryOut(BaseModel):
    trades: int
    wins: int
    win_rate: float
    gross: float
    charges: float
    net: float
    average: float
    median: float
    best: float
    worst: float
    profit_factor: float | None
    max_drawdown: float
    worst_share: float | None
    cost_share: float | None
    exits: dict[str, int]
    by_year: dict[int, float]
    abandoned_orders: int


class RunResponse(BaseModel):
    request: RunRequest
    days: int
    skipped: dict[str, int]
    summary: SummaryOut
    charges: ChargesOut
    #: [day, cumulative net] at each day's close, open legs marked.
    equity: list[tuple[date, float]]
    #: "YYYY-MM" -> net of trades opened that month.
    by_month: dict[str, float]
    trades: list[TradeOut]


def _leg(leg: Leg) -> LegOut:
    return LegOut(
        tag=leg.tag,
        expiry=leg.key.expiry,
        strike=leg.key.strike,
        kind=str(leg.key.kind),
        side="sell" if leg.side < 0 else "buy",
        lots=leg.lots,
        lot_size=leg.lot_size,
        entry_at=leg.entry_ts,
        entry=leg.entry_price,
        stop=leg.stop,
        exit_at=leg.exit_ts,
        exit=leg.exit_price,
        ended=leg.exit_reason,
        pnl=leg.realised if not leg.is_open else 0.0,
        charges=leg.charges.total,
    )


def _trade(trade: Trade) -> TradeOut:
    return TradeOut(
        id=trade.id,
        opened=trade.opened,
        closed=trade.closed,
        ended="+".join(sorted(leg.exit_reason or "open" for leg in trade.legs)),
        gross=trade.gross,
        charges=trade.charges.total,
        net=trade.net,
        legs=[_leg(leg) for leg in trade.legs],
        events=trade.events,
        tags=trade.tags,
        worst=trade.worst,
        best=trade.best,
    )


def _respond(request: RunRequest, result: Result) -> RunResponse:
    s = summarise(result)
    closed = [t for t in result.trades if t.closed is not None]
    total = closed[0].charges if closed else None
    for t in closed[1:]:
        assert total is not None
        total = total + t.charges
    by_month: dict[str, float] = defaultdict(float)
    for t in closed:
        by_month[f"{t.opened:%Y-%m}"] += t.net
    return RunResponse(
        request=request,
        days=result.days,
        skipped=result.skipped,
        summary=SummaryOut(
            trades=s.trades,
            wins=s.wins,
            win_rate=s.win_rate,
            gross=s.gross,
            charges=s.charges,
            net=s.net,
            average=s.average,
            median=s.median,
            best=s.best,
            worst=s.worst,
            profit_factor=s.profit_factor,
            max_drawdown=s.max_drawdown,
            worst_share=s.worst_share,
            cost_share=s.cost_share,
            exits=s.exits,
            by_year=s.by_year,
            abandoned_orders=s.abandoned_orders,
        ),
        charges=ChargesOut(
            brokerage=total.brokerage if total else 0.0,
            stt=total.stt if total else 0.0,
            exchange=total.exchange if total else 0.0,
            sebi=total.sebi if total else 0.0,
            stamp=total.stamp if total else 0.0,
            gst=total.gst if total else 0.0,
            total=total.total if total else 0.0,
        ),
        equity=result.equity,
        by_month=dict(sorted(by_month.items())),
        trades=[_trade(t) for t in result.trades],
    )


@router.post("/run")
def run(request: RunRequest) -> RunResponse:
    if request.end < request.start:
        raise HTTPException(422, "The window ends before it starts.")
    if request.hold == "intraday" and request.exit <= request.entry:
        raise HTTPException(422, "The exit time must be after the entry time.")
    if not set(request.weekdays) & {0, 1, 2, 3, 4}:
        raise HTTPException(422, "Pick at least one weekday to trade.")
    strategy = LegStrategy(request.config())
    costs = CostModel(
        brokerage_per_order=request.brokerage,
        slippage=request.slippage,
        min_slip=request.min_slip,
    )
    with _history(request.underlying) as history:
        result = Engine(history, strategy, costs).run(request.start, request.end)
    return _respond(request, result)


# -------------------------------------------------------------------- replay


class ReplayLeg(BaseModel):
    model_config = STRICT

    expiry: date
    strike: float
    kind: Literal["CE", "PE"]


class ReplayRequest(BaseModel):
    model_config = STRICT

    underlying: str = "NIFTY"
    start: date
    end: date
    legs: list[ReplayLeg] = Field(max_length=8)


class Series(BaseModel):
    label: str
    #: [minute, close] pairs, session minutes only.
    points: list[tuple[datetime, float]]


class ReplayResponse(BaseModel):
    spot: Series
    legs: list[Series]


@router.post("/replay")
def replay(request: ReplayRequest) -> ReplayResponse:
    """Minute closes for the index and each leg across a trade's days."""
    if (request.end - request.start).days > 40:
        raise HTTPException(422, "A replay covers at most 40 days.")
    with _history(request.underlying) as h:
        days = h.trading_days(request.start, request.end)
        spot = [(b.ts, b.close) for d in days for b in h.index_day(d)]
        legs = []
        for leg in request.legs:
            key = OptionKey(leg.expiry, leg.strike, Kind(leg.kind))
            points = [
                (ts, bar.close)
                for d in days
                for ts, bar in sorted(h.contract_day(key, d).items())
            ]
            legs.append(Series(label=str(key), points=points))
    return ReplayResponse(spot=Series(label=request.underlying, points=spot), legs=legs)
