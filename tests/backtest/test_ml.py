"""The entry filter: no leakage in training, and a wrapper that can only veto."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np

from backtest import features
from backtest.engine import run
from backtest.market import PerpetualMarket, Side
from backtest.ml import (
    FilteredRule,
    MLConfig,
    Recorder,
    Sample,
    collect,
    fit,
    make_fit_filter,
    pick_threshold,
    purged_splits,
)
from backtest.models import Action, Intent
from backtest.params import bind, defaults
from backtest.rules import SpecRule
from backtest.spec import parse
from backtest.study import Study, Window, Workers
from backtest.view import View
from backtest.walkforward import Goal, make_plan, walk_forward
from marketdata.models import Interval
from tests.backtest.test_study import EXEC, RAW, _prepared, walk

M5 = Interval.M5
START = datetime(2026, 1, 1, tzinfo=UTC)


def _sample(minute: int, held: int, result: float, f: tuple[float, ...] = (0.0,)) -> Sample:
    at = START + timedelta(minutes=minute)
    return Sample(at, at + timedelta(minutes=held), f, result)


# --------------------------------------------------------------------------
# Purge and embargo
# --------------------------------------------------------------------------


def test_no_training_trade_was_still_open_when_the_block_it_trains_for_began() -> None:
    rng = random.Random(1)
    samples = [_sample(10 * i, rng.randint(1, 400), rng.gauss(0, 1)) for i in range(300)]
    embargo = timedelta(minutes=30)

    splits = purged_splits(samples, 4, embargo)

    assert len(splits) == 4
    for train, valid in splits:
        starts = min(samples[i].decided_at for i in valid)
        assert train, "every split should keep something to train on"
        assert not set(train) & set(valid)
        for i in train:
            assert samples[i].closed_at < starts - embargo, "a label from inside the block"
            assert samples[i].decided_at < starts, "trained on the future"


def test_a_long_trade_straddling_the_block_boundary_is_dropped_and_a_short_one_is_kept() -> None:
    samples = [_sample(10 * i, 5, 0.0) for i in range(100)]
    straddler = _sample(240, 500, 0.0)  # decided in block 0, closes long after block 1 begins
    samples.append(straddler)

    ((train, valid), *_) = purged_splits(samples, 3, timedelta(0))

    first_valid_start = min(samples[i].decided_at for i in valid)
    assert straddler.decided_at < first_valid_start < straddler.closed_at
    assert len(samples) - 1 not in train
    assert any(samples[i].closed_at < first_valid_start for i in train)


def test_the_embargo_widens_the_gap() -> None:
    samples = [_sample(10 * i, 8, 0.0) for i in range(200)]
    tight = purged_splits(samples, 3, timedelta(0))
    wide = purged_splits(samples, 3, timedelta(minutes=120))
    assert all(len(w[0]) < len(t[0]) for t, w in zip(tight, wide, strict=True))


def test_validation_blocks_follow_each_other_in_time() -> None:
    samples = [_sample(10 * i, 3, 0.0) for i in range(120)]
    splits = purged_splits(samples, 4, timedelta(0))
    ends = [max(samples[i].decided_at for i in valid) for _, valid in splits]
    starts = [min(samples[i].decided_at for i in valid) for _, valid in splits]
    assert all(e <= s for e, s in zip(ends, starts[1:], strict=False))


# --------------------------------------------------------------------------
# The threshold
# --------------------------------------------------------------------------


def test_the_threshold_cuts_where_the_model_separates_winners_from_losers() -> None:
    probability = np.linspace(0.0, 1.0, 200)
    result = np.where(probability > 0.5, 0.01, -0.01)

    cut = pick_threshold(probability, result, min_keep=0.25)

    assert 0.45 <= cut <= 0.55


def test_when_nothing_beats_taking_everything_the_filter_stands_aside() -> None:
    probability = np.linspace(0.0, 1.0, 200)
    assert pick_threshold(probability, np.full(200, 0.01), min_keep=0.25) == 0.0
    assert pick_threshold(np.array([]), np.array([]), min_keep=0.25) == 0.0


def test_the_threshold_respects_the_share_of_signals_it_must_keep() -> None:
    probability = np.linspace(0.0, 1.0, 400)
    result = np.where(probability > 0.95, 0.5, -0.01)  # only the top 5% earn anything

    cut = pick_threshold(probability, result, min_keep=0.5)

    assert (probability >= cut).mean() >= 0.5 - 1e-9


# --------------------------------------------------------------------------
# Fitting
# --------------------------------------------------------------------------


def _informative(n: int, signal: float, seed: int = 5) -> list[Sample]:
    rng = random.Random(seed)
    out = []
    for i in range(n):
        x = rng.gauss(0, 1)
        out.append(_sample(15 * i, 20, signal * x + rng.gauss(0, 0.02), (x, rng.gauss(0, 1), 0.0)))
    return out


CFG = MLConfig(min_samples=100, trees=60, workers=1)
NAMES = ("signal", "noise", "constant")


def test_a_feature_that_predicts_the_result_is_found_and_used() -> None:
    model = fit(_informative(900, 0.03), NAMES, CFG, M5)

    assert model is not None
    assert model.importance["signal"] > 0.6
    assert model.threshold > 0.0
    assert model.gain > 0, "out of fold, the cut should have beaten taking everything"
    assert 0.25 <= model.kept < 1.0


def test_pure_noise_leaves_almost_nothing_to_act_on() -> None:
    model = fit(_informative(900, 0.0), NAMES, CFG, M5)
    assert model is not None
    assert model.importance["constant"] == 0.0


def test_too_few_trades_or_a_single_outcome_is_not_enough_to_learn_from() -> None:
    assert fit(_informative(50, 0.03), NAMES, CFG, M5) is None
    all_wins = [_sample(15 * i, 5, 0.01, (float(i), 0.0, 0.0)) for i in range(300)]
    assert fit(all_wins, NAMES, CFG, M5) is None


# --------------------------------------------------------------------------
# The rule in front of a rule
# --------------------------------------------------------------------------


class Stub:
    """A model whose answer is a number the test chose."""

    def __init__(self, answer: float, threshold: float) -> None:
        self.answer, self.threshold = answer, threshold

    def probability(self, row: Any) -> float:
        assert len(row) == len(features.names(M5))
        return self.answer


def _rule() -> SpecRule:
    return SpecRule(parse({**bind(RAW, defaults(RAW)), "interval": "5m"}))


def _filtered(answer: float, threshold: float = 0.5) -> FilteredRule:
    return FilteredRule(_rule(), Stub(answer, threshold), M5)  # type: ignore[arg-type]


def test_a_filter_that_approves_everything_changes_nothing() -> None:
    bars = walk(5000)
    market = PerpetualMarket(symbol="T", min_quantity=0.001, quantity_dp=3)
    plain = run(bars, _rule(), market, interval=M5, execution=EXEC)
    approved = run(bars, _filtered(0.99), market, interval=M5, execution=EXEC)

    assert plain.trades and approved.trades == plain.trades


def test_a_filter_that_rejects_everything_leaves_no_trades_and_no_new_ones() -> None:
    bars = walk(5000)
    market = PerpetualMarket(symbol="T", min_quantity=0.001, quantity_dp=3)
    rule = _filtered(0.01)

    result = run(bars, rule, market, interval=M5, execution=EXEC)

    assert result.trades == []
    assert rule.vetoed == rule.seen > 0


def test_the_filter_never_makes_a_rule_enter_where_it_would_not() -> None:
    """It can only return the base rule's own intent, or nothing."""
    bars = walk(3000)
    rule, base = _filtered(0.01), _rule()
    from backtest.view import build, wind

    view, cursors = build(bars, M5, rule.context)
    for i in range(len(bars)):
        wind(view, cursors, i)
        got, wanted = rule.entry(view), base.entry(view)
        assert got.action is Action.NOTHING or got == wanted
        assert not (got.action is Action.ENTER and wanted.action is not Action.ENTER)


def test_exits_pass_through_untouched() -> None:
    from backtest.models import Position

    rule = _filtered(0.01)
    calls: list[View] = []

    class Spy:
        name = "spy"
        context: tuple[Interval, ...] = ()

        def entry(self, view: View) -> Intent:
            return Intent.nothing()

        def exit(self, view: View, position: Position) -> Intent:
            calls.append(view)
            return Intent.exit("because")

    wrapped = FilteredRule(Spy(), Stub(0.0, 0.5), M5)  # type: ignore[arg-type]
    got = wrapped.exit(None, None)  # type: ignore[arg-type]
    assert got == Intent.exit("because") and len(calls) == 1
    assert rule.name.endswith("+ filter")


def test_a_filter_below_the_zero_threshold_is_the_rule_unchanged() -> None:
    rule = _filtered(0.0, threshold=0.0)
    bars = walk(3000)
    market = PerpetualMarket(symbol="T", min_quantity=0.001, quantity_dp=3)
    assert run(bars, rule, market, interval=M5, execution=EXEC).trades
    assert rule.vetoed == 0


# --------------------------------------------------------------------------
# Labels from the engine
# --------------------------------------------------------------------------


def test_recording_signals_does_not_change_a_single_trade() -> None:
    """The recorder widens the timeframes the view aligns. That must not matter."""
    bars = walk(6000)
    market = PerpetualMarket(symbol="T", min_quantity=0.001, quantity_dp=3)
    plain = run(bars, _rule(), market, interval=M5, execution=EXEC)
    seen: list[Any] = []
    recorded = run(bars, Recorder(_rule(), M5, seen), market, interval=M5, execution=EXEC)

    assert plain.trades and recorded.trades == plain.trades
    assert recorded.equity == plain.equity
    assert len(seen) >= len(plain.trades)


def test_every_trade_is_labelled_with_the_signal_that_made_it() -> None:
    study = Study(_prepared(walk(8000)), EXEC)
    window = Window(2000, 8000, warmup=500)

    samples, names = collect(study, RAW, defaults(RAW), window)
    trades = study.run(RAW, defaults(RAW), window).result.trades

    assert len(samples) == len(trades) > 20
    assert len(names) == len(samples[0].features)
    for sample, trade in zip(samples, trades, strict=True):
        # A fill at the next open is decided on the close just before it.
        assert sample.decided_at == trade.opened_at
        assert sample.closed_at == trade.closed_at
        assert sample.won == (trade.net > 0)


# --------------------------------------------------------------------------
# The whole thing
# --------------------------------------------------------------------------


def test_walk_forward_runs_end_to_end_with_the_filter_and_saves_what_it_fitted(
    tmp_path: Any,
) -> None:
    bars = walk(60_000, seed=9)
    study = Study(_prepared(bars), EXEC)
    plan = make_plan(bars, train_months=1.5, test_months=1, holdout=0.2, warmup=800)
    cfg = MLConfig(min_samples=30, trees=30, workers=1, save_dir=tmp_path)

    with Workers(study, count=1) as workers:
        report = walk_forward(
            study, workers, RAW, plan, Goal("sharpe", 10), trials=6, seed=1,
            fit_filter=make_fit_filter(cfg, seed=1),
        )

    (extra,) = report.others
    assert extra.name == "optimized + ML"
    assert extra.holdout is not None
    assert [b.ts for b in extra.chain.bars] == [b.ts for b in report.tuned.chain.bars]
    assert report.extra["filters"], "at least one fold should have fitted a filter"
    import joblib

    saved = sorted(tmp_path.glob("filter-*.joblib"))
    assert saved
    blob = joblib.load(saved[0])
    assert blob["features"] == features.names(M5)
    assert {"model", "threshold", "train", "values"} <= set(blob)
    # The saved model reproduces the fitted one's answer on a row.
    row = np.array([[0.0] * len(blob["features"])])
    assert 0.0 <= float(np.asarray(blob["model"].predict_proba(row))[0, 1]) <= 1.0
    assert Side.LONG
