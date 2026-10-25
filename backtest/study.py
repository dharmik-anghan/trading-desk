"""Many runs over one series: what a search needs from the engine.

A single backtest loads its bars, builds its market and throws both away. A study
runs hundreds of strategies over the same bars, over different stretches of them,
and the work that does not depend on the strategy is most of a short run's cost. So
it is done once here: the bars are loaded once, the market built once, and the
resampled frames and indicator arrays kept per stretch so that two trials that
differ only in a stop never compute an EMA twice.

Every run still goes through `engine.run`. There is no second engine, no vectorised
shortcut with its own idea of a fill: a trial is the real thing with the cache
turned on, and `tests/backtest/test_study.py` holds the two to identical trades.

**Warm-up is read, not traded.** A window of three months starting in the middle of
a series needs the bars before it for its indicators, or its first weeks are spent
warming up. Those bars are included in the run and the rule is gated off until the
window starts, so the window is judged on what it traded and nothing else. The
warm-up only ever looks backwards, which is what a live rule has anyway.

**Why a process pool.** The engine is a Python loop over bars, so threads would
share one interpreter. Each worker is handed the bars once, at start-up, and after
that a trial crosses the process boundary as a few hundred bytes of JSON and comes
back as a `Score`.
"""

from __future__ import annotations

import math
import multiprocessing
import os
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime
from types import TracebackType
from typing import Any

from backtest.engine import Execution, Result, Rule, run
from backtest.metrics import Metrics, measure
from backtest.models import Intent, Position
from backtest.params import ParamError, bind
from backtest.rules import SpecRule
from backtest.service import Prepared
from backtest.spec import SpecError, parse
from backtest.view import Shared, View
from marketdata.models import Bar, Interval

#: Wraps the rule a spec produces before it is run: the hook the ML filter uses to
#: put a veto in front of the same rule, so a filtered run is the engine's too.
RuleWrapper = Callable[[Rule], Rule]


@dataclass(frozen=True)
class Window:
    """A stretch of the series to trade, by bar index.

    `warmup` bars before `start` are read for the indicators and not traded. They
    are clipped at the start of the series, so a window at the very beginning has
    whatever there is.
    """

    start: int
    end: int
    warmup: int = 0

    def __post_init__(self) -> None:
        if not 0 <= self.start < self.end:
            raise ValueError(f"window {self.start}:{self.end} is empty")

    @property
    def first(self) -> int:
        """The first bar read, warm-up included."""
        return max(0, self.start - self.warmup)

    @property
    def bars(self) -> int:
        return self.end - self.start


@dataclass(frozen=True)
class Score:
    """What a trial came to, small enough to send between processes."""

    trades: int = 0
    wins: int = 0
    total_return: float = 0.0
    sharpe: float = 0.0
    calmar: float | None = None
    max_drawdown: float = 0.0
    annualised: float | None = None
    cost_share: float | None = None
    #: Return of each calendar day, for the Sharpe that deflation is measured on.
    daily: tuple[float, ...] = ()
    #: Why the trial could not be run - a bound spec that does not parse. Empty
    #: for a trial that ran, including one that never traded.
    error: str = ""

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0


@dataclass
class Run:
    """One strategy over one window, in full."""

    window: Window
    #: The bars that were traded, warm-up excluded.
    bars: list[Bar]
    result: Result
    metrics: Metrics
    #: Equity after each traded bar.
    equity: list[float]

    def score(self) -> Score:
        m = self.metrics
        return Score(
            trades=m.trades,
            wins=m.wins,
            total_return=m.total_return,
            sharpe=m.sharpe,
            calmar=m.calmar,
            max_drawdown=m.max_drawdown,
            annualised=m.annualised,
            cost_share=m.cost_share,
            daily=daily_returns(self.bars, self.equity, self.result.capital),
        )


class Windowed:
    """A rule that does not trade before a given bar.

    Its only job is to make warm-up bars unavailable for entries. It sits outside
    whatever else wraps the rule, so a model asked about a signal is never asked
    about one in the warm-up.
    """

    def __init__(self, inner: Rule, first_bar: datetime) -> None:
        self.inner = inner
        self.name = inner.name
        self.context: tuple[Interval, ...] = inner.context
        self._from = first_bar

    def entry(self, view: View) -> Intent:
        bar = view.base.bar
        if bar is None or bar.ts < self._from:
            return Intent.nothing()
        return self.inner.entry(view)

    def exit(self, view: View, position: Position) -> Intent:
        return self.inner.exit(view, position)


class Study:
    """A series loaded once, and any number of strategies run over parts of it."""

    def __init__(self, prepared: Prepared, execution: Execution | None = None) -> None:
        self.prepared = prepared
        self.execution = execution or Execution()
        self._slices: dict[tuple[int, int], tuple[list[Bar], Shared]] = {}

    @property
    def bars(self) -> list[Bar]:
        return self.prepared.bars

    @property
    def interval(self) -> Interval:
        return self.prepared.series.interval

    def full(self) -> Window:
        return Window(0, len(self.bars))

    def run(
        self,
        raw: Mapping[str, Any],
        values: Mapping[str, Any],
        window: Window | None = None,
        *,
        wrap: RuleWrapper | None = None,
        execution: Execution | None = None,
    ) -> Run:
        """Bind `values` into `raw`, and trade it over `window`.

        Raises `SpecError` or `ParamError` for values that do not make a valid
        strategy; `evaluate` turns those into a score instead.
        """
        window = window or self.full()
        if window.end > len(self.bars):
            raise ValueError(f"window ends at {window.end}, past the {len(self.bars)} bars")
        spec = parse({**bind(raw, values), "interval": str(self.interval)})
        rule: Rule = SpecRule(spec)
        if wrap is not None:
            rule = wrap(rule)

        bars, shared = self._slice(window)
        if window.first < window.start:
            rule = Windowed(rule, self.bars[window.start].ts)
        settings = execution or self.execution
        result = run(
            bars, rule, self.prepared.market, interval=self.interval, execution=settings,
            shared=shared,
        )

        # Only the stretch that was traded is judged. Before it the equity is a
        # flat line at the starting capital, and a Sharpe taken over a flat
        # prefix would be diluted by a period in which nothing could happen.
        warm = window.start - window.first
        traded = bars[warm:]
        equity = result.equity[warm:]
        metrics = measure(result.trades, equity, traded, result.capital, self.interval)
        return Run(window, traded, result, metrics, equity)

    def evaluate(
        self, raw: Mapping[str, Any], values: Mapping[str, Any], window: Window | None = None
    ) -> Score:
        """A trial's score, or a score that says why it could not be run."""
        try:
            return self.run(raw, values, window).score()
        except (SpecError, ParamError) as bad:
            return Score(error=str(bad))

    def _slice(self, window: Window) -> tuple[list[Bar], Shared]:
        """The bars a window reads, with the cache that belongs to them.

        The cache is per stretch because an indicator array is indexed by bar
        position: the EMA computed over one slice is the wrong array for another.
        """
        key = (window.first, window.end)
        if key not in self._slices:
            self._slices[key] = (self.bars[window.first : window.end], Shared())
        return self._slices[key]


def daily_returns(
    bars: Sequence[Bar], equity: Sequence[float], capital: float
) -> tuple[float, ...]:
    """The return of each UTC day, from the equity at its last bar.

    Days rather than bars because a statistic about the distribution of returns -
    skew, kurtosis, the number of independent observations - is only meaningful at
    a horizon over which returns are roughly independent. Five-minute returns of a
    strategy that is flat most of the time are mostly zero, and a Sharpe taken over
    a hundred thousand of them claims a hundred thousand observations.
    """
    closes: dict[date, float] = {}
    for bar, value in zip(bars, equity, strict=False):
        closes[bar.ts.date()] = value
    out: list[float] = []
    before = capital
    for value in closes.values():
        out.append(value / before - 1.0 if before > 0 else 0.0)
        before = value
    return tuple(out)


# ---------------------------------------------------------------------------
# The pool
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Job:
    """One trial, as it crosses a process boundary."""

    raw: dict[str, Any]
    values: dict[str, Any]
    window: Window


#: The study each worker process holds, set once by `_start`.
_STUDY: Study | None = None


def _start(prepared: Prepared, execution: Execution) -> None:
    global _STUDY
    _STUDY = Study(prepared, execution)


def _score(job: Job) -> Score:
    assert _STUDY is not None, "the worker was not started with a study"
    return _STUDY.evaluate(job.raw, job.values, job.window)


class Workers:
    """Trials run in parallel, over a series every worker already holds.

    One worker runs in this process with no pool at all, which is what tests and
    small studies want: it is deterministic, debuggable, and spares the cost of
    starting interpreters that would each be sent three years of bars.
    """

    def __init__(self, study: Study, count: int | None = None) -> None:
        self.study = study
        self.count = count if count is not None else max(1, (os.cpu_count() or 2) - 1)
        self._pool: ProcessPoolExecutor | None = None

    def __enter__(self) -> Workers:
        if self.count > 1:
            self._pool = ProcessPoolExecutor(
                max_workers=self.count,
                # Spawn, not fork: forking a process that holds a DuckDB handle
                # and numpy threads is how a parallel run hangs on one machine
                # and works on another.
                mp_context=multiprocessing.get_context("spawn"),
                initializer=_start,
                initargs=(self.study.prepared, self.study.execution),
            )
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=True, cancel_futures=True)
            self._pool = None

    def score(self, jobs: Sequence[Job]) -> list[Score]:
        """Each job's score, in the order given."""
        if self._pool is None:
            return [self.study.evaluate(j.raw, j.values, j.window) for j in jobs]
        return list(self._pool.map(_score, jobs, chunksize=1))


def finite(value: float | None, otherwise: float = 0.0) -> float:
    """A number that can be compared and averaged, whatever it was handed."""
    return value if value is not None and math.isfinite(value) else otherwise
