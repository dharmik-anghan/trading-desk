"""Choosing parameters without grading them on the data that chose them.

A parameter search on three years of bars returns the settings that happened to
fit those three years, and reports how well they fit - which is a statement about
the search, not the strategy. This module does the search the way it would have to
be done to be usable: on a past window, with the answer judged on the window after
it, and rolled forward.

    |---- train ----|- test -|
              |---- train ----|- test -|
                        |---- train ----|- test -|                |- holdout -|

Each fold searches its train window with Optuna's TPE sampler and runs the best
settings once on the test window that follows. The test windows, chained in order,
are the **out-of-sample curve**: what the whole procedure - search, then trade what
it found, then search again - would have returned. That is the number to trust.

The last slice of the data is a **holdout** that no search ever sees: not as train,
not as test, not to choose between two procedures. It is traded once, with settings
searched on the window just before it, and reported separately. It is the only
figure here that has not influenced a decision about the method.

Three checks on whether a result is a plateau or a peak:

**Deflated Sharpe.** The probability that the best trial's Sharpe is real after
that many tries. See `backtest/significance.py`.

**Parameter stability.** The best settings are nudged one notch each way in every
parameter and run again. A good setting sits in a region where its neighbours score
about the same; one that falls apart a notch away is a spike the search found, and
a spike does not repeat.

**Train against test.** The same settings' score on the window they were chosen on
and on the one after. The gap is what overfitting costs, fold by fold.
"""

from __future__ import annotations

import math
from bisect import bisect_left
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

import optuna

from backtest.metrics import Metrics, measure
from backtest.models import Trade
from backtest.params import Param, declared, defaults, feasible
from backtest.significance import deflated_sharpe, probabilistic_sharpe, sharpe
from backtest.study import Job, RuleWrapper, Run, Score, Study, Window, Workers
from marketdata.models import Bar

optuna.logging.set_verbosity(optuna.logging.WARNING)

#: A month, for window arithmetic. Calendar months differ by ten percent and
#: nothing here is sensitive to that.
MONTH = timedelta(days=30.4375)

#: What a trial that cannot be scored is told to the sampler as. Finite, because
#: TPE ranks by value and an infinity breaks its quantile split, and far below any
#: real objective so that it always ranks last.
PENALTY = -1.0e3

OBJECTIVES = ("sharpe", "calmar", "net_dd")


# ---------------------------------------------------------------------------
# The geometry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Fold:
    """One round of search-then-trade."""

    number: int
    train: Window
    test: Window


@dataclass(frozen=True)
class Plan:
    """Every window a study uses, laid out before anything is run.

    Computed from the bars' times alone so that it can be inspected, and tested for
    overlap, without running anything.
    """

    folds: tuple[Fold, ...]
    #: The window the holdout's settings are searched on: the same length as a
    #: fold's train window, ending where the holdout begins.
    final_train: Window
    holdout: Window
    #: Everything before the holdout: what a search or a comparison may look at.
    research: Window

    def windows(self) -> list[Window]:
        out = [w for f in self.folds for w in (f.train, f.test)]
        return [*out, self.final_train, self.holdout]


def make_plan(
    bars: Sequence[Bar],
    *,
    train_months: float = 12,
    test_months: float = 3,
    step_months: float | None = None,
    holdout: float = 0.15,
    warmup: int = 0,
) -> Plan:
    """Lay folds over the bars, with the last `holdout` fraction set aside.

    Test windows may not overlap one another - their trades are chained into one
    curve and a bar traded twice would be counted twice - so a step shorter than
    the test is refused.
    """
    step_months = step_months if step_months is not None else test_months
    if step_months < test_months:
        raise ValueError("the step cannot be shorter than the test window; tests would overlap")
    if not 0 < holdout < 0.5:
        raise ValueError("the holdout is a fraction of the data between 0 and 0.5")
    n = len(bars)
    if n < 100:
        raise ValueError(f"{n} bars is not enough to lay folds over")

    cut = int(n * (1 - holdout))
    times = [b.ts for b in bars]

    def at(moment: datetime) -> int:
        return bisect_left(times, moment)

    train, test, step = train_months * MONTH, test_months * MONTH, step_months * MONTH
    folds: list[Fold] = []
    start = times[0]
    while True:
        train_end = start + train
        test_end = train_end + test
        a, b, c = at(start), at(train_end), at(test_end)
        if c > cut or b <= a or c <= b:
            break
        folds.append(Fold(len(folds), Window(a, b, warmup), Window(b, c, warmup)))
        start += step
    if not folds:
        raise ValueError(
            f"{train_months:g} + {test_months:g} months does not fit before the holdout; "
            "shorten the windows or load more history"
        )

    last = at(times[cut] - train)
    return Plan(
        folds=tuple(folds),
        final_train=Window(last, cut, warmup),
        holdout=Window(cut, n, warmup),
        research=Window(0, cut, warmup),
    )


# ---------------------------------------------------------------------------
# The objective
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Goal:
    """What a search is trying to maximise, and what it will not accept."""

    objective: str = "sharpe"
    #: A trial with fewer closed trades than this is not scored: a Sharpe built
    #: from nine trades is the first thing a search finds and the last thing to
    #: believe.
    min_trades: int = 30
    #: For "net_dd": how many points of return one point of drawdown costs.
    drawdown_penalty: float = 1.0

    def __post_init__(self) -> None:
        if self.objective not in OBJECTIVES:
            raise ValueError(f"{self.objective!r} is not an objective; pick {OBJECTIVES}")

    def value(self, score: Score) -> float | None:
        """The score as one number to maximise, or None for a trial that does not count."""
        if score.error or score.trades < self.min_trades:
            return None
        if self.objective == "sharpe":
            return score.sharpe
        if self.objective == "calmar":
            return score.calmar if score.calmar is not None else 0.0
        return score.total_return - self.drawdown_penalty * score.max_drawdown


# ---------------------------------------------------------------------------
# One search
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Trial:
    number: int
    values: dict[str, Any]
    score: Score
    #: None when the trial did not count: too few trades, or it would not run.
    value: float | None


@dataclass(frozen=True)
class Search:
    """Everything one search tried, and what it picked."""

    window: Window
    trials: tuple[Trial, ...]
    best: Trial | None
    #: Suggestions the constraints ruled out before any run.
    rejected: int = 0

    @property
    def counted(self) -> list[Trial]:
        return [t for t in self.trials if t.value is not None]


def _suggest(trial: optuna.Trial, param: Param) -> Any:
    if param.kind == "int":
        return trial.suggest_int(
            param.name, int(param.low), int(param.high), step=int(param.step or 1), log=param.log
        )
    if param.kind == "float":
        return trial.suggest_float(
            param.name, param.low, param.high, step=param.step, log=param.log
        )
    return trial.suggest_categorical(param.name, list(param.choices))


def _key(values: Mapping[str, Any]) -> tuple[tuple[str, Any], ...]:
    return tuple(sorted(values.items()))


def optimize(
    workers: Workers,
    raw: Mapping[str, Any],
    window: Window,
    goal: Goal,
    *,
    trials: int = 100,
    seed: int = 1,
    batch: int | None = None,
) -> Search:
    """Search `raw`'s parameters over `window` with a TPE sampler.

    Suggestions are taken `batch` at a time and scored in parallel, then reported
    back in order, so a run is reproducible from its seed whatever the number of
    workers. The constant-liar setting stops a batch from proposing the same corner
    of the space `batch` times over while it waits for answers.

    `trials` is a budget of distinct settings actually run. A suggestion the
    constraints rule out costs nothing, and a repeat is answered from memory.
    """
    params = declared(raw)
    if not params:
        raise ValueError("this strategy declares no parameters to search")

    sampler = optuna.samplers.TPESampler(
        seed=seed,
        multivariate=True,
        constant_liar=True,
        n_startup_trials=max(10, min(30, trials // 4)),
    )
    study = optuna.create_study(direction="maximize", sampler=sampler)
    batch = batch or max(1, workers.count)
    seen: dict[tuple[tuple[str, Any], ...], Trial] = {}
    done: list[Trial] = []
    rejected = 0
    # Enough attempts that a tight constraint cannot loop forever.
    attempts, limit = 0, trials * 25

    while len(done) < trials and attempts < limit:
        asked: list[tuple[optuna.Trial, dict[str, Any]]] = []
        for _ in range(min(batch, trials - len(done))):
            attempts += 1
            t = study.ask()
            values = {name: _suggest(t, p) for name, p in params.items()}
            if not feasible(raw, values):
                rejected += 1
                study.tell(t, state=optuna.trial.TrialState.FAIL)
                continue
            asked.append((t, values))

        fresh: list[dict[str, Any]] = []
        for _, values in asked:
            if _key(values) not in seen and values not in fresh:
                fresh.append(values)
        scores = workers.score([Job(dict(raw), v, window) for v in fresh])
        for values, score in zip(fresh, scores, strict=True):
            value = goal.value(score)
            trial = Trial(len(done), values, score, value)
            seen[_key(values)] = trial
            done.append(trial)

        for t, values in asked:
            known = seen[_key(values)]
            if known.value is not None:
                study.tell(t, known.value)
            else:
                # Graded below the floor, so that of two failed trials the one
                # closer to enough trades ranks higher and the sampler is nudged
                # towards the region that trades at all.
                study.tell(t, PENALTY + min(1.0, known.score.trades / max(1, goal.min_trades)))

    ranked = sorted(
        (t for t in done if t.value is not None),
        key=lambda t: (t.value or 0.0, t.score.trades),
        reverse=True,
    )
    return Search(window, tuple(done), ranked[0] if ranked else None, rejected)


# ---------------------------------------------------------------------------
# Plateau or peak
# ---------------------------------------------------------------------------

#: A best setting whose neighbours average less than this share of its score is
#: flagged as a spike.
SHARP_BELOW = 0.7


@dataclass(frozen=True)
class Stability:
    """How the best setting's neighbours score."""

    best: float
    #: Each neighbour that could be run: (what moved, its score or None).
    neighbours: tuple[tuple[str, float | None], ...]

    @property
    def mean(self) -> float | None:
        counted = [v for _, v in self.neighbours if v is not None]
        return sum(counted) / len(counted) if counted else None

    @property
    def ratio(self) -> float | None:
        """Neighbours' average over the best. None when the best is not positive."""
        mean = self.mean
        if mean is None or self.best <= 0:
            return None
        return mean / self.best

    @property
    def uncounted(self) -> int:
        """Neighbours that traded too little to score - a cliff, not a plateau."""
        return sum(1 for _, v in self.neighbours if v is None)

    @property
    def sharp(self) -> bool:
        ratio = self.ratio
        if self.uncounted and self.uncounted >= len(self.neighbours) / 2:
            return True
        return ratio is not None and ratio < SHARP_BELOW


def stability(
    workers: Workers,
    raw: Mapping[str, Any],
    window: Window,
    goal: Goal,
    best: Trial,
) -> Stability | None:
    """Run the best setting's neighbours on the window it was chosen on."""
    params = declared(raw)
    moves: list[tuple[str, dict[str, Any]]] = []
    for name, param in params.items():
        for direction in (-1, +1):
            nudged = param.nudge(best.values[name], direction)
            if nudged is None:
                continue
            values = {**best.values, name: nudged}
            if feasible(raw, values):
                moves.append((f"{name}={nudged:g}", values))
    if not moves or best.value is None:
        return None
    scores = workers.score([Job(dict(raw), values, window) for _, values in moves])
    return Stability(
        best=best.value,
        neighbours=tuple(
            (label, goal.value(score)) for (label, _), score in zip(moves, scores, strict=True)
        ),
    )


# ---------------------------------------------------------------------------
# Chaining test windows into one curve
# ---------------------------------------------------------------------------


class Chain:
    """Runs on consecutive windows, joined into one equity curve.

    Each window starts with the money the previous one ended with, so the curve is
    one account traded through time rather than several accounts glued end to end.
    Sizing that compounds therefore compounds across folds, as it would live.
    """

    def __init__(self, study: Study) -> None:
        self.study = study
        self.capital = study.execution.capital
        self.start = study.execution.capital
        self.runs: list[Run] = []

    def run(
        self,
        raw: Mapping[str, Any],
        values: Mapping[str, Any],
        window: Window,
        wrap: RuleWrapper | None = None,
    ) -> Run:
        execution = replace(self.study.execution, capital=self.capital)
        done = self.study.run(raw, values, window, wrap=wrap, execution=execution)
        self.capital = done.equity[-1] if done.equity else self.capital
        self.runs.append(done)
        return done

    @property
    def trades(self) -> list[Trade]:
        return [t for r in self.runs for t in r.result.trades]

    @property
    def bars(self) -> list[Bar]:
        return [b for r in self.runs for b in r.bars]

    @property
    def equity(self) -> list[float]:
        return [v for r in self.runs for v in r.equity]

    def metrics(self) -> Metrics:
        return measure(self.trades, self.equity, self.bars, self.start, self.study.interval)

    def daily(self) -> tuple[float, ...]:
        from backtest.study import daily_returns

        return daily_returns(self.bars, self.equity, self.start)


# ---------------------------------------------------------------------------
# The whole procedure
# ---------------------------------------------------------------------------


@dataclass
class Variant:
    """A way of trading a window: the tuned settings, or with a filter on top."""

    name: str
    chain: Chain
    holdout: Run | None = None


@dataclass
class FoldReport:
    fold: Fold
    values: dict[str, Any]
    train_value: float | None
    test_value: float | None
    test: Score
    trials: int
    rejected: int
    stability: Stability | None
    #: Deflated Sharpe of the best trial on its train window.
    deflated: float | None
    train_sharpe_daily: float
    #: Each variant's test score by name, for variants beyond the plain one.
    extra: dict[str, Score] = field(default_factory=dict)
    note: str = ""


@dataclass
class Report:
    plan: Plan
    goal: Goal
    folds: list[FoldReport]
    #: Settings searched on the window before the holdout.
    final_values: dict[str, Any]
    final_search: Search
    #: The tuned strategy, walked forward.
    tuned: Variant
    #: The strategy at its declared defaults - what was there before any search -
    #: traded on exactly the same windows.
    baseline: Variant
    #: The defaults over the whole research window, which they were free to be
    #: picked on. In-sample by construction.
    baseline_research: Run
    #: Variants beyond those, such as the entry filter.
    others: list[Variant] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def oos_deflated(self) -> float | None:
        values = [f.deflated for f in self.folds if f.deflated is not None]
        return sum(values) / len(values) if values else None

    def oos_probability(self) -> float:
        """Probability the stitched out-of-sample Sharpe is above zero."""
        return probabilistic_sharpe(self.tuned.chain.daily())


@dataclass(frozen=True)
class Fitted:
    """An entry filter fitted to a train window, ready to put in front of a rule."""

    wrap: RuleWrapper
    info: dict[str, Any] = field(default_factory=dict)


#: Fits an entry filter to a train window and says how to apply it to the next
#: stretch - the hook `backtest/ml.py` plugs into. Returns None when there is not
#: enough to learn from.
FitFilter = Callable[[Study, Mapping[str, Any], dict[str, Any], Window], Fitted | None]


def walk_forward(
    study: Study,
    workers: Workers,
    raw: Mapping[str, Any],
    plan: Plan,
    goal: Goal,
    *,
    trials: int = 100,
    seed: int = 1,
    fit_filter: FitFilter | None = None,
    progress: Callable[[str], None] | None = None,
) -> Report:
    """Search each fold, trade what it found on the window after, then the holdout."""
    say = progress or (lambda _msg: None)
    tuned = Variant("optimized", Chain(study))
    base = Variant("hand-tuned", Chain(study))
    filtered = Variant("optimized + ML", Chain(study)) if fit_filter else None
    start = defaults(raw)
    reports: list[FoldReport] = []
    log: list[dict[str, Any]] = []
    filters: list[dict[str, Any]] = []

    for fold in plan.folds:
        search = optimize(workers, raw, fold.train, goal, trials=trials, seed=seed + fold.number)
        best = search.best
        _log_search(log, fold.number, "train", search)
        note = ""
        extra: dict[str, Score] = {}

        # The hand-tuned strategy is traded on every test window whatever the
        # search found, so the two curves cover the same bars.
        base.chain.run(raw, start, fold.test)

        if best is None:
            # Nothing cleared the minimum number of trades, so there is nothing
            # to trade. The fold sits out rather than taking an arbitrary setting.
            note = "no setting traded enough"
            values, test_score = dict(start), Score()
            found, deflated = None, None
        else:
            values = best.values
            test_score = tuned.chain.run(raw, values, fold.test).score()
            found = stability(workers, raw, fold.train, goal, best)
            sharpes = [sharpe(t.score.daily) for t in search.counted]
            deflated = deflated_sharpe(best.score.daily, sharpes) if len(sharpes) > 2 else None

            if filtered is not None and fit_filter is not None:
                fitted = fit_filter(study, raw, values, fold.train)
                if fitted is None:
                    # Too little to learn from: the filter stands aside and the
                    # fold is traded as the plain tuned strategy.
                    done = filtered.chain.run(raw, values, fold.test)
                    note = "too few trades to fit the filter"
                else:
                    done = filtered.chain.run(raw, values, fold.test, wrap=fitted.wrap)
                    filters.append({"fold": fold.number, **fitted.info})
                extra[filtered.name] = done.score()

        reports.append(
            FoldReport(
                fold=fold,
                values=values,
                train_value=best.value if best else None,
                test_value=goal.value(test_score) if best else None,
                test=test_score,
                trials=len(search.trials),
                rejected=search.rejected,
                stability=found,
                deflated=deflated,
                train_sharpe_daily=sharpe(best.score.daily) if best else 0.0,
                extra=extra,
                note=note,
            )
        )
        say(
            f"fold {fold.number + 1}/{len(plan.folds)}: {len(search.trials)} trials, "
            f"train {_fmt(best.value if best else None)}, test {_fmt(reports[-1].test_value)}"
        )

    final = optimize(workers, raw, plan.final_train, goal, trials=trials, seed=seed + 1000)
    _log_search(log, -1, "final", final)
    final_values = final.best.values if final.best else dict(start)

    tuned.holdout = Chain(study).run(raw, final_values, plan.holdout)
    base.holdout = Chain(study).run(raw, start, plan.holdout)
    if filtered is not None and fit_filter is not None and final.best is not None:
        fitted = fit_filter(study, raw, final_values, plan.final_train)
        filtered.holdout = Chain(study).run(
            raw, final_values, plan.holdout, wrap=fitted.wrap if fitted else None
        )
        if fitted is not None:
            filters.append({"fold": -1, **fitted.info})

    baseline_research = Chain(study).run(raw, start, plan.research)
    return Report(
        plan=plan,
        goal=goal,
        folds=reports,
        final_values=final_values,
        final_search=final,
        tuned=tuned,
        baseline=base,
        baseline_research=baseline_research,
        others=[filtered] if filtered else [],
        extra={"trials": log, "filters": filters},
    )


def _log_search(log: list[dict[str, Any]], fold: int, kind: str, search: Search) -> None:
    for t in search.trials:
        log.append(
            {
                "fold": fold,
                "kind": kind,
                "trial": t.number,
                **{f"p_{k}": v for k, v in t.values.items()},
                "objective": t.value,
                "trades": t.score.trades,
                "sharpe": t.score.sharpe,
                "return": t.score.total_return,
                "max_dd": t.score.max_drawdown,
                "error": t.score.error,
            }
        )


def _fmt(value: float | None) -> str:
    return "n/a" if value is None or not math.isfinite(value) else f"{value:.2f}"

