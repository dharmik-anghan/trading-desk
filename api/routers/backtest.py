"""Running a built strategy over stored history.

The strategy arrives as the tree the builder assembled - see `backtest/spec.py` -
and everything that decides what a result means arrives with it: which series,
which window, how much capital, what leverage, how much slippage to assume. A
result that cannot be reproduced from its own request is a result nobody can
check, so the request is echoed back with it.

Two things are deliberately not returned in full. Three years of five-minute bars
is 315,361 equity points, which is megabytes of JSON to draw a line a thousand
pixels wide, so the curve is thinned - but every figure computed from it, the
drawdown especially, is computed on the whole series first. And a rule that traded
forty thousand times sends the most recent few hundred with a count of the rest.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from api.charting import CandleResponse, LineResponse
from api.deps import bar_store, require_bar_service
from backtest.engine import Execution
from backtest.lines import compute
from backtest.metrics import Metrics
from backtest.reality import Reality
from backtest.reality import check as reality_check
from backtest.resample import resample
from backtest.service import Floors, NoBars, Series, prepare, run_stored, stored_interval
from backtest.spec import SpecError, StrategySpec, parse
from backtest.study import Study
from marketdata import BarService, Interval
from marketdata.models import Bar
from marketdata.store import BarStore

router = APIRouter(tags=["backtest"], prefix="/api/backtest")

#: Points on the returned curve. Enough to draw three years without a visible
#: step, few enough to send.
CURVE_POINTS = 1200

#: Trades sent back. Raised well above what anyone scrolls, because the point of
#: a trade log is to be searched and totalled rather than read - and a run that
#: made two thousand trades should hand over two thousand rather than a sample
#: that cannot be added up.
MAX_TRADES = 5000


class RunRequest(BaseModel):
    """A strategy, and everything needed to say what running it meant."""

    spec: dict[str, Any]
    source: str = "binance"
    symbol: str = "BTCUSDT"
    #: The bar size to trade. Resampled from what is stored when it is longer.
    interval: str = "5m"
    days: int = Field(default=1095, ge=1, le=3650)
    capital: float = Field(default=1000.0, gt=0)
    leverage: float = Field(default=1.0, gt=0, le=150)
    slippage_bps: float = Field(default=1.0, ge=0, le=100)
    maker_entry: bool = False
    #: "equity" - a fraction of the account; "quantity" - a fixed lot;
    #: "notional" - a fixed amount of money at work.
    sizing: str = "equity"
    risk: float = Field(default=1.0, gt=0, le=1)
    quantity: float = Field(default=0.0, ge=0)
    notional: float = Field(default=0.0, ge=0)
    #: The venue's floors, so a run refuses a size nobody could place.
    min_quantity: float = Field(default=0.001, ge=0)
    min_notional: float = Field(default=0.0, ge=0)
    quantity_dp: int = Field(default=3, ge=0, le=8)
    #: Whose funding to charge. Empty means none, which is only honest for an
    #: instrument that has none.
    funding_source: str = "binance"


class TradeOut(BaseModel):
    """One round trip, in full.

    Everything needed to reconstruct it: when, which way, at what prices, how
    much, why it was taken, why it ended, and where every unit of money went.
    """

    side: str
    opened_at: str
    closed_at: str
    entry: float
    exit_price: float
    quantity: float
    #: Position value at entry, in the quote currency.
    notional: float
    why: str
    #: The condition that opened it, in the words it was built with.
    entry_reason: str
    #: What closed it - the exit condition, or the stop, target or liquidation.
    exit_reason: str
    gross: float
    fees: float
    funding: float
    slippage: float
    net: float
    #: Net as a fraction of the margin the position required.
    net_pct: float
    #: How long it was held, in bars of the traded size.
    bars_held: float


class SideOut(BaseModel):
    trades: int
    wins: int
    win_rate: float
    gross: float
    net: float


class MetricsOut(BaseModel):
    trades: int
    wins: int
    win_rate: float
    total_return: float
    buy_and_hold: float
    beat_holding: bool
    max_drawdown: float
    annualised: float | None
    sharpe: float
    calmar: float | None
    gross: float
    fees: float
    funding: float
    slippage: float
    cost_share: float | None
    exposure: float
    best: float
    worst: float
    endings: dict[str, int]
    by_side: dict[str, SideOut]
    average_bars_held: float


class RunResponse(BaseModel):
    name: str
    #: The strategy in words, as it was built.
    reads: str
    symbol: str
    source: str
    interval: str
    bars: int
    started: str | None
    ended: str | None
    capital: float
    final: float
    metrics: MetricsOut
    #: Thinned for drawing. `bars` says how many there really were.
    curve: list[list[float]]
    trades: list[TradeOut]
    trades_total: int
    #: Positions closed and reopened the other way on the same signal.
    reversals: int
    #: Setups that armed a resting order, and those price never reached.
    armed: int
    expired_unfilled: int
    skipped_too_small: int
    skipped_unaffordable: int
    #: Anything a reader has to know to judge the numbers.
    caveats: list[str]


class WindowRequest(BaseModel):
    source: str
    symbol: str
    interval: str
    start: str
    end: str
    #: The strategy, so its indicators can be drawn. Optional: a chart without
    #: them is still a chart.
    spec: dict[str, Any] | None = None


class WindowResponse(BaseModel):
    source: str
    symbol: str
    interval: str
    candles: list[CandleResponse]
    lines: list[LineResponse] = []


def _service(request: Request) -> BarService:
    return require_bar_service(request, "there is nothing to test on")


@router.post("/candles", response_model=WindowResponse)
def candles(request: Request, body: WindowRequest) -> WindowResponse:
    """The bars over one window, with the strategy's own indicators on them.

    A result says a trade made money; this is what lets you see whether it was a
    trade anybody would have taken. The window is named by time rather than by a
    count of days, because the caller is asking about a particular trade and not
    about a period.

    Read from the store only. A backtest is over history that has already been
    fetched, and going to a source here could return bars that differ from the
    ones the run was computed on - which would make the picture disagree with the
    numbers beside it.

    The indicators are computed through the same `View` the engine runs on, not
    recomputed some other way and certainly not in the browser. A line drawn by
    different code from the one that made the decision is a line that can be
    subtly wrong exactly where it matters - at the crossing.
    """
    try:
        size = Interval(body.interval)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{body.interval} is not a bar size") from None
    try:
        from_at = datetime.fromisoformat(body.start)
        to_at = datetime.fromisoformat(body.end)
    except ValueError:
        raise HTTPException(status_code=400, detail="start and end are ISO timestamps") from None
    if to_at <= from_at:
        raise HTTPException(status_code=400, detail="the window ends before it begins")

    spec = None
    if body.spec:
        try:
            spec = parse({**body.spec, "interval": body.interval})
        except SpecError:
            # A chart is still worth drawing for a strategy that will not run.
            spec = None

    service = _service(request)
    held = stored_interval(service, body.source, body.symbol, size)
    wanted = spec.indicators() if spec else []
    warmup = _warmup(wanted)

    # Read back far enough that the indicators have converged before the window
    # begins. An exponential average started later differs from one started
    # earlier, and the difference is what would show up as a line that crosses a
    # bar too soon.
    earliest = from_at - timedelta(seconds=size.seconds * warmup)
    days = max(1, int((datetime.now(UTC) - earliest).total_seconds() / 86400) + 1)
    bars = service.stored(body.source, body.symbol, held, days=days).bars
    if held != size:
        bars = resample(bars, size)
    bars = [b for b in bars if b.ts >= earliest]

    inside = [i for i, b in enumerate(bars) if from_at <= b.ts <= to_at]
    lines = _lines(bars, size, spec, inside) if spec and wanted else []

    return WindowResponse(
        source=body.source,
        symbol=body.symbol,
        interval=str(size),
        candles=[
            CandleResponse(
                at=bars[i].ts.isoformat(),
                open=bars[i].open,
                high=bars[i].high,
                low=bars[i].low,
                close=bars[i].close,
                volume=bars[i].volume,
            )
            for i in inside
        ],
        lines=lines,
    )


#: How far back to compute an indicator before the window being drawn.
#:
#: An exponential average never forgets its start completely, but it forgets it
#: fast: after twelve periods the seed is worth less than a thousandth, and after
#: three hundred bars the difference is below what any price is quoted to. Enough
#: that the drawn line and the line the engine read are the same line.
MIN_WARMUP = 300


def _warmup(indicators: list[tuple[str, int, Interval | None]]) -> int:
    longest = max((length for _, length, _ in indicators), default=0)
    return max(MIN_WARMUP, longest * 12)


def _lines(
    bars: list[Bar], size: Interval, spec: StrategySpec, inside: list[int]
) -> list[LineResponse]:
    """Each indicator's value at each bar of the window.

    Through `backtest.lines`, which the trading desk's own chart also uses: two
    charts in the same app disagreeing about where an EMA 20 sits would be worse
    than either being wrong alone.
    """
    return [
        LineResponse(
            label=drawn.label,
            name=drawn.name,
            length=drawn.length,
            interval=str(drawn.interval) if drawn.interval else None,
            on_price=drawn.on_price,
            values=drawn.values,
        )
        for drawn in compute(bars, size, spec.indicators(), inside)
    ]


@router.post("/run", response_model=RunResponse)
def run_backtest(request: Request, body: RunRequest) -> RunResponse:
    """Run one strategy over one series. See `backtest/service.py`."""
    try:
        spec = parse({**body.spec, "interval": body.interval})
    except SpecError as bad:
        # The builder's own message, which says what to change.
        raise HTTPException(status_code=400, detail=str(bad)) from None

    try:
        size = Interval(body.interval)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{body.interval} is not a bar size") from None

    execution = Execution(
        capital=body.capital,
        sizing=body.sizing,
        risk=body.risk,
        quantity=body.quantity,
        notional=body.notional,
        leverage=body.leverage,
        slippage_bps=body.slippage_bps,
        maker_entry=body.maker_entry,
    )
    store = bar_store(request)
    try:
        done = run_stored(
            _service(request),
            store if isinstance(store, BarStore) else None,
            spec,
            Series(body.source, body.symbol, size, body.days),
            execution,
            funding_source=body.funding_source,
            floors=Floors(body.min_quantity, body.min_notional, body.quantity_dp),
        )
    except NoBars as missing:
        raise HTTPException(status_code=404, detail=str(missing)) from None

    result, bars = done.result, done.bars
    return RunResponse(
        name=spec.name,
        reads=_sentence(spec),
        symbol=body.symbol,
        source=body.source,
        interval=str(size),
        bars=len(bars),
        started=result.started.isoformat() if result.started else None,
        ended=result.ended.isoformat() if result.ended else None,
        capital=result.capital,
        final=result.final,
        metrics=_metrics_out(done.metrics),
        curve=_thinned(bars, result.equity),
        trades=[_trade_out(t, size, execution) for t in result.trades[-MAX_TRADES:]],
        trades_total=len(result.trades),
        reversals=result.reversals,
        armed=result.armed,
        expired_unfilled=result.expired_unfilled,
        skipped_too_small=result.skipped_too_small,
        skipped_unaffordable=result.skipped_unaffordable,
        caveats=done.caveats,
    )


class CheckRequest(RunRequest):
    #: Random-entry runs the real result is ranked against.
    random_runs: int = Field(default=200, ge=20, le=1000)


class CheckOut(BaseModel):
    name: str
    passed: bool
    detail: str


class PeriodOut(BaseModel):
    label: str
    trades: int
    total_return: float
    win_rate: float


class CheckResponse(BaseModel):
    """Whether a result is the strategy or the market and luck. See backtest/reality.py."""

    verdict: str
    checks: list[CheckOut]
    trades: int
    total_return: float
    p_value: float
    random_runs: int
    random_median: float
    random_best: float
    random_trades: int
    periods: list[PeriodOut]
    stressed_return: float


def _check_out(r: Reality) -> CheckResponse:
    return CheckResponse(
        verdict=r.verdict,
        checks=[CheckOut(name=c.name, passed=c.passed, detail=c.detail) for c in r.checks],
        trades=r.trades,
        total_return=r.total_return,
        p_value=r.p_value,
        random_runs=r.random_runs,
        random_median=r.random_median,
        random_best=r.random_best,
        random_trades=r.random_trades,
        periods=[
            PeriodOut(
                label=p.label, trades=p.trades, total_return=p.total_return, win_rate=p.win_rate
            )
            for p in r.periods
        ],
        stressed_return=r.stressed_return,
    )


@router.post("/check", response_model=CheckResponse)
def check_backtest(request: Request, body: CheckRequest) -> CheckResponse:
    """Run the strategy against random entries, two periods and doubled costs."""
    raw = {**body.spec, "interval": body.interval}
    try:
        parse(raw)
        size = Interval(body.interval)
    except SpecError as bad:
        raise HTTPException(status_code=400, detail=str(bad)) from None
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{body.interval} is not a bar size") from None

    execution = Execution(
        capital=body.capital,
        sizing=body.sizing,
        risk=body.risk,
        quantity=body.quantity,
        notional=body.notional,
        leverage=body.leverage,
        slippage_bps=body.slippage_bps,
        maker_entry=body.maker_entry,
    )
    store = bar_store(request)
    try:
        prepared = prepare(
            _service(request),
            store if isinstance(store, BarStore) else None,
            Series(body.source, body.symbol, size, body.days),
            funding_source=body.funding_source,
            floors=Floors(body.min_quantity, body.min_notional, body.quantity_dp),
        )
    except NoBars as missing:
        raise HTTPException(status_code=404, detail=str(missing)) from None
    workers = max(1, min(6, (os.cpu_count() or 2) - 1))
    return _check_out(
        reality_check(Study(prepared, execution), raw, runs=body.random_runs, workers=workers)
    )


def _sentence(spec: Any) -> str:
    """The strategy read back, so a result says what produced it."""
    parts: list[str] = []
    if spec.trigger is not None:
        t = spec.trigger
        where = f"the candle {t.ago} back" if t.ago else "that candle"
        cushion = f" by {t.buffer_bps:g}bp" if t.buffer_bps else ""
        parts.append(
            f"Enter only if price breaks the {t.field} of {where}{cushion} "
            f"within {t.within} bar{'s' if t.within != 1 else ''}"
        )
    if spec.sessions:
        during = " or ".join(s.describe() for s in spec.sessions)
        parts.append(f"Only during {during}")
        if spec.close_outside_session:
            parts.append("Close whatever is open when the session ends")
    if spec.long_entry:
        parts.append(f"Long when {spec.long_entry.describe()}")
    if spec.short_entry:
        parts.append(f"Short when {spec.short_entry.describe()}")
    if spec.long_exit:
        parts.append(f"Close longs when {spec.long_exit.describe()}")
    if spec.short_exit:
        parts.append(f"Close shorts when {spec.short_exit.describe()}")
    return ". ".join(parts)


def _thinned(bars: list[Any], equity: list[float]) -> list[list[float]]:
    """The curve, at a size worth sending.

    Each bucket keeps its lowest point rather than its last. A curve thinned by
    sampling loses exactly the spikes a drawdown is made of, and a drawdown that
    disappears when a chart is drawn is the one figure nobody should have to
    take on trust.
    """
    if not equity:
        return []
    step = max(1, len(equity) // CURVE_POINTS)
    out: list[list[float]] = []
    for at in range(0, len(equity), step):
        window = equity[at : at + step]
        lowest = min(range(len(window)), key=lambda k: window[k])
        index = at + lowest
        out.append([bars[index].ts.timestamp(), round(equity[index], 2)])
    # The end matters: it is the result.
    last = len(equity) - 1
    if out[-1][0] != bars[last].ts.timestamp():
        out.append([bars[last].ts.timestamp(), round(equity[last], 2)])
    return out


def _metrics_out(m: Metrics) -> MetricsOut:
    return MetricsOut(
        trades=m.trades,
        wins=m.wins,
        win_rate=m.win_rate,
        total_return=m.total_return,
        buy_and_hold=m.buy_and_hold,
        beat_holding=m.beat_holding,
        max_drawdown=m.max_drawdown,
        annualised=m.annualised,
        sharpe=m.sharpe,
        calmar=m.calmar,
        gross=m.gross,
        fees=m.fees,
        funding=m.funding,
        slippage=m.slippage,
        cost_share=m.cost_share,
        exposure=m.exposure,
        best=m.best,
        worst=m.worst,
        endings=m.endings,
        average_bars_held=m.average_bars_held,
        by_side={
            side: SideOut(
                trades=s.trades, wins=s.wins, win_rate=s.win_rate, gross=s.gross, net=s.net
            )
            for side, s in m.by_side.items()
        },
    )


def _trade_out(t: Any, interval: Interval, execution: Execution) -> TradeOut:
    notional = t.quantity * t.entry
    margin = notional / execution.leverage if execution.leverage else notional
    return TradeOut(
        side=str(t.side),
        opened_at=t.opened_at.isoformat(),
        closed_at=t.closed_at.isoformat(),
        entry=t.entry,
        exit_price=t.exit_price,
        quantity=t.quantity,
        notional=notional,
        why=str(t.why),
        entry_reason=t.entry_reason,
        exit_reason=t.exit_reason,
        gross=t.gross,
        fees=t.costs.fees,
        funding=t.costs.funding,
        slippage=t.costs.slippage,
        net=t.net,
        # Against the margin the position tied up rather than against the whole
        # account: at 10x a 1% move is 10% of what was committed, and reporting
        # it as 1% would describe a different trade.
        net_pct=t.net / margin if margin else 0.0,
        bars_held=t.bars_held / interval.seconds,
    )
