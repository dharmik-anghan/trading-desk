"""A study's trials are the engine's runs, with the cache turned on.

The whole value of a search is that its numbers mean what a normal backtest's do.
So the first test here is the one that matters: the same strategy, run through the
study and run through `run_stored`, trades identically, to the cent.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from backtest.engine import Execution
from backtest.market import PerpetualMarket
from backtest.params import bind, defaults
from backtest.service import Floors, Prepared, execute, prepare, run_stored
from backtest.service import Series as RunSeries
from backtest.spec import parse
from backtest.study import Job, Study, Window, Workers, daily_returns
from marketdata import BarService, Interval
from marketdata.models import Bar, Series
from marketdata.store import BarStore

START = datetime(2026, 1, 1, tzinfo=UTC)
M5 = Interval.M5


def _ema(n: Any) -> dict[str, Any]:
    return {"kind": "indicator", "name": "ema", "length": n}


#: EMA cross, long and short, with both lengths searchable.
RAW: dict[str, Any] = {
    "name": "ema cross",
    "params": {
        "fast": {"type": "int", "low": 3, "high": 30, "default": 8},
        "slow": {"type": "int", "low": 10, "high": 80, "default": 30},
        "stop": {"type": "float", "low": 0.2, "high": 3.0, "default": 1.0},
    },
    "constraints": [{"lt": ["fast", "slow"]}],
    "long_entry": {
        "op": "crosses_above", "left": _ema({"param": "fast"}), "right": _ema({"param": "slow"})
    },
    "short_entry": {
        "op": "crosses_below", "left": _ema({"param": "fast"}), "right": _ema({"param": "slow"})
    },
    "long_exit": {
        "op": "crosses_below", "left": _ema({"param": "fast"}), "right": _ema({"param": "slow"})
    },
    "short_exit": {
        "op": "crosses_above", "left": _ema({"param": "fast"}), "right": _ema({"param": "slow"})
    },
    "stop": {"kind": "percent", "value": {"param": "stop"}},
}


def walk(n: int, seed: int = 11) -> list[Bar]:
    """A wandering price with trends in it, so a crossover has something to do."""
    rng = random.Random(seed)
    price, drift = 100.0, 0.0
    out: list[Bar] = []
    for i in range(n):
        if i % 400 == 0:
            drift = rng.gauss(0, 0.0004)
        o = price
        price = max(1.0, price * (1 + drift + rng.gauss(0, 0.002)))
        out.append(
            Bar(
                ts=START + timedelta(seconds=M5.seconds * i),
                open=o,
                high=max(o, price) * (1 + abs(rng.gauss(0, 0.0008))),
                low=min(o, price) * (1 - abs(rng.gauss(0, 0.0008))),
                close=price,
                volume=10 + rng.random(),
            )
        )
    return out


class _NoSource:
    """The service wants a source to be constructed with; a stored read never uses one."""


def _prepared(bars: list[Bar]) -> Prepared:
    market = PerpetualMarket(symbol="TESTUSDT", min_quantity=0.001, quantity_dp=3)
    return Prepared(RunSeries("test", "TESTUSDT", M5, 60), bars, market)


EXEC = Execution(capital=1000.0, leverage=2.0, slippage_bps=1.0)


def _stored(bars: list[Bar]) -> BarService:
    store = BarStore(":memory:")
    series = Series("test", "TESTUSDT", M5)
    store.write(series, bars)
    service = BarService(
        store,
        source=_NoSource(),  # type: ignore[arg-type]
        now=lambda: bars[-1].ts + timedelta(days=1),
        binance=_NoSource(),  # type: ignore[arg-type]
    )
    return service


def test_a_trial_through_the_study_trades_exactly_as_run_stored_does() -> None:
    bars = walk(6000)
    values = defaults(RAW)
    service = _stored(bars)

    spec = parse({**bind(RAW, values), "interval": "5m"})
    wanted = RunSeries("test", "TESTUSDT", M5, 60)
    floors = Floors(0.001, 0.0, 3)
    reference = run_stored(service, None, spec, wanted, EXEC, floors=floors)
    trial = Study(prepare(service, None, wanted, floors=floors), EXEC).run(RAW, values)

    assert len(reference.result.trades) > 10, "the series should give the rule something to do"
    assert trial.result.trades == reference.result.trades
    assert trial.equity == reference.result.equity
    assert trial.metrics == reference.metrics


def test_the_shared_cache_does_not_leak_one_trial_into_the_next() -> None:
    """Trials reuse indicator arrays. They must not reuse anything else."""
    bars = walk(5000)
    shared = Study(_prepared(bars), EXEC)
    grid = [
        {"fast": 5, "slow": 20, "stop": 1.0},
        {"fast": 5, "slow": 40, "stop": 1.0},  # shares EMA 5 with the one before
        {"fast": 5, "slow": 20, "stop": 0.5},  # shares both EMAs, differs in the stop
        {"fast": 12, "slow": 40, "stop": 2.0},
    ]
    for values in grid:
        cached = shared.run(RAW, values)
        fresh = Study(_prepared(bars), EXEC).run(RAW, values)
        assert cached.result.trades == fresh.result.trades, values
        assert cached.equity == fresh.equity, values


def test_a_second_trial_reuses_the_indicator_arrays_of_the_first() -> None:
    study = Study(_prepared(walk(3000)), EXEC)
    study.run(RAW, {"fast": 5, "slow": 20, "stop": 1.0})
    ((bars_, shared),) = study._slices.values()
    held = dict(shared.lines(M5))

    study.run(RAW, {"fast": 5, "slow": 20, "stop": 0.4})

    assert set(held) == set(shared.lines(M5))
    for key, line in held.items():
        assert shared.lines(M5)[key] is line, f"{key} was recomputed"
    assert bars_ is study._slices[(0, 3000)][0]


def test_a_window_trades_only_inside_itself_and_reads_the_bars_before_it() -> None:
    bars = walk(6000)
    study = Study(_prepared(bars), EXEC)
    window = Window(start=3000, end=5000, warmup=800)

    trial = study.run(RAW, defaults(RAW), window)

    assert trial.result.trades, "the window should have traded"
    assert all(t.opened_at >= bars[3000].ts for t in trial.result.trades)
    last_close = bars[4999].ts + timedelta(seconds=M5.seconds)
    assert all(t.closed_at <= last_close for t in trial.result.trades)
    assert len(trial.equity) == window.bars == len(trial.bars)
    assert trial.bars[0] is bars[3000]


def test_warm_up_makes_the_window_start_trading_with_indicators_already_formed() -> None:
    """Without it the first trades of a window wait for the indicators to form."""
    bars = walk(6000)
    study = Study(_prepared(bars), EXEC)
    cold = study.run(RAW, defaults(RAW), Window(3000, 3300, warmup=0))
    warm = study.run(RAW, defaults(RAW), Window(3000, 3300, warmup=1000))

    # The cold window's slow EMA (30) needs 30 bars, so nothing can open in them.
    assert all(t.opened_at >= bars[3030].ts for t in cold.result.trades)
    first_warm = min((t.opened_at for t in warm.result.trades), default=None)
    first_cold = min((t.opened_at for t in cold.result.trades), default=None)
    assert first_warm is not None
    assert first_cold is None or first_warm <= first_cold


def test_a_bad_binding_is_a_score_with_a_reason_not_a_crash() -> None:
    study = Study(_prepared(walk(500)), EXEC)
    score = study.evaluate(RAW, {"fast": 5, "slow": 20, "stop": 99.0})
    assert score.error and "outside" in score.error
    assert score.trades == 0


def test_the_pool_scores_what_the_study_scores() -> None:
    study = Study(_prepared(walk(4000)), EXEC)
    jobs = [
        Job(RAW, {"fast": f, "slow": s, "stop": 1.0}, Window(1500, 3500, warmup=500))
        for f, s in [(5, 20), (8, 30), (12, 50)]
    ]
    with Workers(study, count=1) as serial:
        expected = serial.score(jobs)
    with Workers(study, count=2) as pool:
        got = pool.score(jobs)

    assert got == expected
    assert any(score.trades for score in got)


def test_daily_returns_chain_to_the_final_equity() -> None:
    bars = walk(2000)
    equity = [1000.0 + i for i in range(2000)]

    daily = daily_returns(bars, equity, 1000.0)

    total = 1.0
    for r in daily:
        total *= 1 + r
    assert total * 1000.0 == pytest.approx(equity[-1])
    assert len(daily) == len({b.ts.date() for b in bars})


def test_execute_is_what_run_stored_returns() -> None:
    bars = walk(2500)
    spec = parse({**bind(RAW, defaults(RAW)), "interval": "5m"})
    done = execute(_prepared(bars), spec, EXEC)
    assert done.result.bars == 2500
    assert done.metrics.trades == len(done.result.trades)
