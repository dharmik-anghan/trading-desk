"""Running a strategy over a stored series: everything between a spec and a result.

The engine (`backtest/engine.py`) takes bars and returns trades; this finds the
bars, builds what they are traded against, runs it, and says what the result
should be read with. The API and any script call this - so an engine in another
language slots in behind `run` here without anything above it changing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from backtest.engine import Execution, Result, run
from backtest.market import FundingSchedule, PerpetualMarket
from backtest.metrics import Metrics, measure
from backtest.resample import resample
from backtest.rules import SpecRule
from backtest.spec import StrategySpec
from backtest.view import Shared
from marketdata import BarService, Interval
from marketdata.models import Bar
from marketdata.store import BarStore


class NoBars(LookupError):
    """Nothing stored to run on. The message says what to backfill."""


@dataclass(frozen=True)
class Series:
    """Which bars to run over."""

    source: str
    symbol: str
    interval: Interval
    days: int


@dataclass(frozen=True)
class Floors:
    """The venue's minimums, so a run refuses a size nobody could place."""

    min_quantity: float = 0.001
    min_notional: float = 0.0
    quantity_dp: int = 3


@dataclass(frozen=True)
class Backtest:
    bars: list[Bar]
    result: Result
    metrics: Metrics
    caveats: list[str] = field(default_factory=list)


def stored_interval(service: BarService, source: str, symbol: str, wanted: Interval) -> Interval:
    """The stored series to build `wanted` out of.

    Prefers the exact size when it is held and otherwise the longest stored size
    that divides it - resampling up is arithmetic, and down is impossible.
    """
    held = {
        series.interval
        for series, count in service.held()
        if series.source == source and series.symbol == symbol and count
    }
    if wanted in held:
        return wanted
    usable = [i for i in held if wanted.seconds % i.seconds == 0 and i.seconds < wanted.seconds]
    if not usable:
        return wanted
    return max(usable, key=lambda i: i.seconds)


def load_bars(service: BarService, series: Series) -> list[Bar]:
    """The series' bars, built from the shortest size held when that is shorter.

    Anything longer than what is stored is resampled rather than fetched, so the
    two cannot disagree about a bar boundary.
    """
    held = stored_interval(service, series.source, series.symbol, series.interval)
    bars = service.stored(series.source, series.symbol, held, days=series.days).bars
    if not bars:
        raise NoBars(
            f"No {held} bars stored for {series.symbol} from {series.source}. "
            "Run scripts/backfill_bars.py first."
        )
    return resample(bars, series.interval) if held != series.interval else bars


def _funding(
    store: BarStore | None, funding_source: str, series: Series, start: Bar
) -> tuple[FundingSchedule | None, list[str]]:
    if not funding_source or store is None:
        return None, []
    # From the first bar rather than from `days` ago by the wall clock. The two
    # are not the same thing - a store that ends last week gives a window that
    # ended last week - and taking the clock's answer fetched funding for a
    # period the run does not cover, which is to say none of it.
    rates = store.read_funding(funding_source, series.symbol, start=start.ts)
    if not rates:
        return None, [
            "No funding history for this symbol, so none was charged. A perpetual "
            "held for days pays it, so this reads better than it would have been"
        ]
    caveats = []
    if funding_source != series.source or series.source != "shark":
        caveats.append(
            f"Funding is {funding_source}'s, charged every 8 hours. The venue "
            "publishes none of its own, so this is a proxy - and it settles every "
            "4 or 8 hours there depending on the contract"
        )
    return FundingSchedule.of(funding_source, rates), caveats


@dataclass(frozen=True)
class Prepared:
    """A series loaded and ready to be traded: the bars and what they trade against.

    Everything `run_stored` did before a rule was involved. Split out because a
    study runs hundreds of rules over one series, and reading three years of bars
    from DuckDB and building the market for each of them is most of the cost of a
    short trial.
    """

    series: Series
    bars: list[Bar]
    market: PerpetualMarket
    #: What loading found worth saying - a funding proxy, a missing history.
    caveats: tuple[str, ...] = ()


def prepare(
    service: BarService,
    store: BarStore | None,
    series: Series,
    *,
    funding_source: str = "",
    floors: Floors | None = None,
) -> Prepared:
    """Load `series` and build its market. Raises `NoBars` when there is nothing stored."""
    floors = floors or Floors()
    bars = load_bars(service, series)
    funding, caveats = _funding(store, funding_source, series, bars[0])
    market = PerpetualMarket(
        symbol=series.symbol,
        funding=funding,
        min_quantity=floors.min_quantity,
        min_notional=floors.min_notional,
        quantity_dp=floors.quantity_dp,
    )
    return Prepared(series, bars, market, tuple(caveats))


def execute(
    prepared: Prepared, spec: StrategySpec, execution: Execution, *, shared: Shared | None = None
) -> Backtest:
    """Run `spec` over prepared bars, and say what the result should be read with."""
    series, bars = prepared.series, prepared.bars
    result = run(
        bars,
        SpecRule(spec),
        prepared.market,
        interval=series.interval,
        execution=execution,
        shared=shared,
    )
    metrics = measure(result.trades, result.equity, bars, result.capital, series.interval)
    caveats = list(prepared.caveats)
    caveats.extend(result.caveats)
    # A side that was declared and never traded is the kind of thing a result
    # should say out loud. It usually means the strategy contradicts itself, or
    # that every signal for that side was consumed by the other side's exit.
    for side, declared in (("long", spec.long_entry), ("short", spec.short_entry)):
        if declared is not None and side not in metrics.by_side:
            caveats.append(
                f"This strategy declares {side} entries but never took one, so the result "
                f"is the other side alone"
            )
    if execution.maker_entry:
        caveats.append(
            "Entries are resting limit orders, filled only where a bar traded through "
            "them. Exits still pay the taker fee"
        )
    return Backtest(bars, result, metrics, caveats)


def run_stored(
    service: BarService,
    store: BarStore | None,
    spec: StrategySpec,
    series: Series,
    execution: Execution,
    *,
    funding_source: str = "",
    floors: Floors | None = None,
) -> Backtest:
    """Run `spec` over a stored series. Raises `NoBars` when there is nothing to run on."""
    prepared = prepare(service, store, series, funding_source=funding_source, floors=floors)
    return execute(prepared, spec, execution)
