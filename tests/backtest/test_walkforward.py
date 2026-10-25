"""Walk-forward: the windows never overlap, the holdout is never searched, and the
search is reproducible.

The first two are the properties the whole procedure is worth nothing without, so
they are tested directly rather than inferred from a result looking sensible.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from backtest.engine import Execution
from backtest.market import PerpetualMarket
from backtest.params import defaults
from backtest.service import Prepared
from backtest.service import Series as RunSeries
from backtest.study import Score, Study, Window, Workers
from backtest.walkforward import (
    Chain,
    Goal,
    Stability,
    make_plan,
    optimize,
    stability,
    walk_forward,
)
from marketdata.models import Bar, Interval
from tests.backtest.test_study import RAW, walk

M5 = Interval.M5
START = datetime(2024, 1, 1, tzinfo=UTC)


def _ts_bars(n: int, interval: Interval = Interval.H1) -> list[Bar]:
    """Flat bars with real times: enough to lay folds over."""
    return [
        Bar(
            ts=START + timedelta(seconds=interval.seconds * i),
            open=100, high=101, low=99, close=100, volume=1.0,
        )
        for i in range(n)
    ]


def _study(bars: list[Bar]) -> Study:
    market = PerpetualMarket(symbol="TESTUSDT", min_quantity=0.001, quantity_dp=3)
    prepared = Prepared(RunSeries("test", "TESTUSDT", M5, 60), bars, market)
    return Study(prepared, Execution(capital=1000.0, leverage=2.0, slippage_bps=1.0))


# --------------------------------------------------------------------------
# The geometry
# --------------------------------------------------------------------------


def test_every_window_precedes_the_one_that_tests_it_and_nothing_overlaps() -> None:
    bars = _ts_bars(3 * 365 * 24)  # three years of hours

    plan = make_plan(bars, train_months=12, test_months=3, holdout=0.15)

    assert len(plan.folds) >= 4
    for fold in plan.folds:
        assert fold.train.end <= fold.test.start, "a fold tests on data it trained on"
        assert fold.test.end <= plan.holdout.start, "a fold reaches into the holdout"
        assert fold.train.end <= plan.holdout.start
    tests = [f.test for f in plan.folds]
    for earlier, later in zip(tests, tests[1:], strict=False):
        assert earlier.end <= later.start, "two test windows share a bar"
    assert plan.final_train.end == plan.holdout.start == plan.research.end
    assert plan.holdout.end == len(bars)


def test_the_holdout_is_the_last_slice_and_about_the_size_asked_for() -> None:
    bars = _ts_bars(10_000)
    plan = make_plan(bars, train_months=2, test_months=1, holdout=0.2)
    assert plan.holdout.start == 8000
    assert plan.holdout.bars == 2000


def test_windows_have_the_length_they_were_asked_for() -> None:
    bars = _ts_bars(3 * 365 * 24)
    plan = make_plan(bars, train_months=12, test_months=3)
    for fold in plan.folds:
        assert fold.train.bars == pytest.approx(12 * 30.4375 * 24, abs=2)
        assert fold.test.bars == pytest.approx(3 * 30.4375 * 24, abs=2)
    assert plan.final_train.bars == pytest.approx(12 * 30.4375 * 24, abs=2)


def test_folds_step_by_the_step_and_a_step_shorter_than_the_test_is_refused() -> None:
    bars = _ts_bars(3 * 365 * 24)
    stepped = make_plan(bars, train_months=12, test_months=3, step_months=6)
    starts = [f.train.start for f in stepped.folds]
    assert starts[1] - starts[0] == pytest.approx(6 * 30.4375 * 24, abs=2)

    with pytest.raises(ValueError, match="tests would overlap"):
        make_plan(bars, train_months=12, test_months=3, step_months=1)


def test_a_series_too_short_for_one_fold_says_so() -> None:
    with pytest.raises(ValueError, match="does not fit before the holdout"):
        make_plan(_ts_bars(2000), train_months=12, test_months=3)


@pytest.mark.parametrize("holdout", [0.0, 0.5, -0.1])
def test_a_holdout_outside_zero_to_a_half_is_refused(holdout: float) -> None:
    with pytest.raises(ValueError, match="holdout"):
        make_plan(_ts_bars(5000), train_months=1, test_months=1, holdout=holdout)


def test_warm_up_is_carried_by_every_window_without_moving_its_edges() -> None:
    bars = _ts_bars(10_000)
    bare = make_plan(bars, train_months=2, test_months=1)
    warm = make_plan(bars, train_months=2, test_months=1, warmup=500)
    for a, b in zip(bare.windows(), warm.windows(), strict=True):
        assert (a.start, a.end) == (b.start, b.end)
        assert b.warmup == 500


# --------------------------------------------------------------------------
# The search
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def synthetic() -> tuple[Study, list[Bar]]:
    bars = walk(40_000, seed=21)
    return _study(bars), bars


GOAL = Goal(objective="sharpe", min_trades=10)


def test_the_same_seed_gives_the_same_search(synthetic: tuple[Study, list[Bar]]) -> None:
    study, _ = synthetic
    window = Window(2000, 12_000, warmup=500)
    with Workers(study, count=1) as workers:
        a = optimize(workers, RAW, window, GOAL, trials=14, seed=5)
        b = optimize(workers, RAW, window, GOAL, trials=14, seed=5)
        c = optimize(workers, RAW, window, GOAL, trials=14, seed=6)

    assert [t.values for t in a.trials] == [t.values for t in b.trials]
    assert [t.values for t in a.trials] != [t.values for t in c.trials]
    assert a.best is not None and a.best == b.best


def test_a_batch_of_workers_gives_the_same_search_as_one(
    synthetic: tuple[Study, list[Bar]],
) -> None:
    """Parallelism changes how long it takes; it must not change what is found.

    Compared at the same batch size, because the batch is part of the sampler's
    schedule - what is asked before the answers come back.
    """
    study, _ = synthetic
    window = Window(2000, 9000, warmup=500)
    with Workers(study, count=1) as serial:
        one = optimize(serial, RAW, window, GOAL, trials=12, seed=3, batch=3)
    with Workers(study, count=3) as pool:
        many = optimize(pool, RAW, window, GOAL, trials=12, seed=3, batch=3)

    assert [t.values for t in one.trials] == [t.values for t in many.trials]
    assert [t.score for t in one.trials] == [t.score for t in many.trials]


def test_constraints_keep_infeasible_settings_from_ever_being_run(
    synthetic: tuple[Study, list[Bar]],
) -> None:
    study, _ = synthetic
    with Workers(study, count=1) as workers:
        found = optimize(workers, RAW, Window(2000, 9000, warmup=500), GOAL, trials=20, seed=2)

    assert found.rejected > 0, "fast < slow should rule out part of this space"
    assert all(t.values["fast"] < t.values["slow"] for t in found.trials)
    assert len(found.trials) == 20
    assert not any(t.score.error for t in found.trials)


def test_a_trial_below_the_minimum_trades_does_not_count_and_cannot_win(
    synthetic: tuple[Study, list[Bar]],
) -> None:
    study, _ = synthetic
    strict = Goal(objective="sharpe", min_trades=10_000)
    with Workers(study, count=1) as workers:
        found = optimize(workers, RAW, Window(2000, 6000, warmup=500), strict, trials=6, seed=1)

    assert found.best is None
    assert all(t.value is None for t in found.trials)


@pytest.mark.parametrize(
    ("objective", "expected"),
    [("sharpe", 1.5), ("calmar", 4.0), ("net_dd", 0.3 - 0.5 * 0.1)],
)
def test_objectives_read_the_figure_they_name(objective: str, expected: float) -> None:
    score = Score(trades=50, sharpe=1.5, calmar=4.0, total_return=0.3, max_drawdown=0.1)
    goal = Goal(objective=objective, min_trades=30, drawdown_penalty=0.5)
    assert goal.value(score) == pytest.approx(expected)
    assert goal.value(Score(trades=29, sharpe=9.0)) is None
    assert goal.value(Score(trades=50, error="x")) is None


def test_an_unknown_objective_is_refused() -> None:
    with pytest.raises(ValueError, match="not an objective"):
        Goal(objective="profit")


# --------------------------------------------------------------------------
# Plateau or peak
# --------------------------------------------------------------------------


def test_a_best_setting_with_matching_neighbours_is_a_plateau() -> None:
    plateau = Stability(1.0, (("a=1", 0.9), ("a=3", 1.0), ("b=1", 0.95), ("b=3", 0.85)))
    assert plateau.ratio == pytest.approx(0.925)
    assert not plateau.sharp


def test_a_best_setting_that_falls_apart_a_notch_away_is_a_spike() -> None:
    spike = Stability(2.0, (("a=1", 0.2), ("a=3", 0.4), ("b=1", 0.1), ("b=3", 0.5)))
    assert spike.ratio == pytest.approx(0.15)
    assert spike.sharp


def test_neighbours_that_do_not_trade_at_all_are_a_cliff() -> None:
    cliff = Stability(1.0, (("a=1", None), ("a=3", None), ("b=1", 1.0), ("b=3", 0.9)))
    assert cliff.uncounted == 2 and cliff.sharp


def test_stability_nudges_each_parameter_both_ways(synthetic: tuple[Study, list[Bar]]) -> None:
    study, _ = synthetic
    window = Window(2000, 9000, warmup=500)
    with Workers(study, count=1) as workers:
        found = optimize(workers, RAW, window, GOAL, trials=10, seed=4)
        assert found.best is not None
        result = stability(workers, RAW, window, GOAL, found.best)

    assert result is not None
    moved = {label.split("=")[0] for label, _ in result.neighbours}
    assert moved <= {"fast", "slow", "stop"}
    assert len(result.neighbours) <= 6


# --------------------------------------------------------------------------
# The whole procedure
# --------------------------------------------------------------------------


def test_chained_windows_are_one_account_traded_through_time() -> None:
    bars = walk(8000, seed=4)
    study = _study(bars)
    chain = Chain(study)
    first = chain.run(RAW, defaults(RAW), Window(1000, 3000, warmup=400))
    second = chain.run(RAW, defaults(RAW), Window(3000, 5000, warmup=400))

    assert second.result.capital == pytest.approx(first.equity[-1])
    assert chain.metrics().trades == len(first.result.trades) + len(second.result.trades)
    assert len(chain.equity) == 4000


def test_no_search_ever_sees_the_holdout_and_no_fold_tests_on_its_own_train() -> None:
    """Every window a worker was asked to score, recorded and checked."""
    bars = walk(60_000, seed=9)
    study = _study(bars)
    plan = make_plan(bars, train_months=1.5, test_months=1, holdout=0.2, warmup=800)

    scored: list[Window] = []

    class Spy(Workers):
        def score(self, jobs: Any) -> list[Score]:
            scored.extend(j.window for j in jobs)
            return super().score(jobs)

    with Spy(study, count=1) as workers:
        report = walk_forward(study, workers, RAW, plan, GOAL, trials=8, seed=1)

    assert scored, "the search should have scored something"
    assert all(w.end <= plan.holdout.start for w in scored), "a search saw the holdout"
    test_starts = [f.test.start for f in plan.folds]
    train_ends = {w.end for w in scored}
    assert train_ends <= {f.train.end for f in plan.folds} | {plan.final_train.end}
    assert all(f.train.end <= s for f, s in zip(plan.folds, test_starts, strict=True))

    assert report.tuned.holdout is not None and report.baseline.holdout is not None
    held = report.tuned.holdout
    assert all(t.opened_at >= bars[plan.holdout.start].ts for t in held.result.trades)
    assert len(report.folds) == len(plan.folds)
    assert len(report.tuned.chain.runs) + sum(1 for f in report.folds if f.note) == len(plan.folds)


def test_the_hand_tuned_baseline_trades_the_same_bars_as_the_tuned_strategy() -> None:
    bars = walk(60_000, seed=9)
    study = _study(bars)
    plan = make_plan(bars, train_months=1.5, test_months=1, holdout=0.2, warmup=800)
    with Workers(study, count=1) as workers:
        report = walk_forward(study, workers, RAW, plan, GOAL, trials=6, seed=2)

    assert [b.ts for b in report.baseline.chain.bars] == [b.ts for b in report.tuned.chain.bars]
    assert report.baseline_research.window == plan.research
    assert report.oos_probability() >= 0.0
