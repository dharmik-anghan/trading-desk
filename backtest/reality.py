"""Whether a backtest's profit is the strategy or the market and luck.

A rule that made money on three years of Bitcoin has not shown that it can pick
entries: Bitcoin rose, and a rule that is simply long a lot of the time makes money
in a rising market with no skill at all. So the question asked here is not "did it
make money" but "did it make more than the same trading done at random".

Four checks, each a yes or no, and a verdict from them:

  against random   the same stops, targets, exits, sides and about the same number
                   of trades, with the entry bar chosen at random, run many times.
                   Where the real result ranks among them is the p-value: the share
                   of random runs that did at least as well.
  enough trades    below a hundred, a result is an anecdote.
  both periods     the first 70% of the history and the last 30% each have to make
                   money, so one lucky stretch cannot carry it.
  costs doubled    fees and slippage twice as large. An edge that only exists at the
                   quoted cost is one bad fill away from not existing.

Every run goes through `Study`, the same engine the Backtesting page uses.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from backtest.engine import Rule
from backtest.models import Action, Intent
from backtest.rules import SpecRule
from backtest.spec import StrategySpec
from backtest.study import Run, Study, Window
from backtest.view import View

#: Random runs, and the share of them that may beat the real one for it to count
#: as better than luck.
RANDOM_RUNS = 200
LUCK_LEVEL = 0.05
MIN_TRADES = 100
HOLDOUT = 0.3
#: Days of bars read before a window starts, so its indicators are warm.
WARMUP_DAYS = 30


class RandomEntry(SpecRule):
    """The same rule with its entry signal replaced by a coin.

    Exits, stops, targets, the trigger and the sizing are the spec's own, so the
    only thing that differs from the real run is where the trades start. Sides are
    drawn in the proportion the real run took them, so a long-only strategy is
    compared with long-only randomness and not with a coin that also shorts.
    """

    def __init__(self, spec: StrategySpec, seed: int, chance: float, long_share: float) -> None:
        super().__init__(spec)
        self._rng = random.Random(seed)
        self._chance = chance
        self._long_share = long_share

    def entry(self, view: View) -> Intent:
        intent = super().entry(view)
        spec = self.spec
        # A random bar often has no stop on the right side of it - "the low of
        # two candles ago" is above the price when the market has been falling.
        # The rule drops such a stop and trades without one, which would leave the
        # random run holding an unmanaged position the real one never had. A setup
        # whose stop or target cannot be placed is skipped, as it is everywhere
        # else a stop is the rule's definition of risk.
        if intent.action is Action.ENTER and (
            (spec.stop is not None and intent.stop is None)
            or (spec.target is not None and intent.target is None)
        ):
            return Intent.nothing()
        return intent

    def _wants(self, view: View) -> tuple[bool, bool]:
        spec = self.spec
        if self._rng.random() >= self._chance:
            return False, False
        if spec.long_entry is None:
            return False, True
        if spec.short_entry is None:
            return True, False
        long = self._rng.random() < self._long_share
        return long, not long


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    #: What was measured, short enough for a table cell.
    detail: str


@dataclass(frozen=True)
class Period:
    label: str
    trades: int
    total_return: float
    win_rate: float


@dataclass(frozen=True)
class Reality:
    verdict: str  # "pass", "weak" or "fail"
    checks: list[Check]
    trades: int
    total_return: float
    #: Share of random runs that did at least as well as the real one.
    p_value: float
    random_runs: int
    random_median: float
    random_best: float
    random_trades: int
    periods: list[Period]
    stressed_return: float


def _chance(study: Study, raw: Mapping[str, Any], trades: int, exposure: float) -> float:
    """Entry chance per flat bar that gives about `trades` trades.

    Starts from the real run's own rate and corrects once against a trial run,
    because a trigger that never fills, or a stop that skips a setup, makes the
    first guess land a long way off.
    """
    bars = len(study.bars)
    flat = max(1.0, bars * (1.0 - exposure))
    chance = min(1.0, max(1e-5, trades / flat))
    for _ in range(3):
        got = sum(_trial(study, raw, s, chance, 0.5).metrics.trades for s in (0, 1)) / 2
        if got <= 0 or abs(got - trades) <= 0.1 * trades:
            break
        chance = min(1.0, chance * trades / got)
    return chance


def _trial(
    study: Study, raw: Mapping[str, Any], seed: int, chance: float, long_share: float
) -> Run:
    def wrap(rule: Rule) -> Rule:
        assert isinstance(rule, SpecRule)
        return RandomEntry(rule.spec, seed, chance, long_share)

    return study.run(raw, {}, wrap=wrap)


#: The study each worker process holds, set once when it starts.
_STUDY: Study | None = None


def _hold(study: Study) -> None:
    global _STUDY
    _STUDY = study


def _random_return(args: tuple[Mapping[str, Any], int, float, float]) -> float:
    raw, seed, chance, long_share = args
    assert _STUDY is not None
    return float(_trial(_STUDY, raw, seed, chance, long_share).metrics.total_return)


def check(
    study: Study,
    raw: Mapping[str, Any],
    *,
    runs: int = RANDOM_RUNS,
    seed: int = 1,
    workers: int | None = 1,
    progress: Callable[[str], None] | None = None,
) -> Reality:
    """Run the checks on the strategy `raw` over the series `study` holds."""
    say = progress or (lambda _: None)
    say("the real run")
    real = study.run(raw, {})
    m = real.metrics

    bars = len(study.bars)
    warmup = int(WARMUP_DAYS * 86400 / study.interval.seconds)
    cut = int(bars * (1 - HOLDOUT))
    periods: list[Period] = []
    for label, window in (
        ("first 70%", Window(0, cut)),
        ("last 30%", Window(cut, bars, warmup)),
    ):
        r = study.run(raw, {}, window).metrics
        periods.append(Period(label, r.trades, r.total_return, r.win_rate))

    say("costs doubled")
    market = study.prepared.market
    pricey = replace(
        study.prepared,
        market=replace(market, maker_fee=market.maker_fee * 2, taker_fee=market.taker_fee * 2),
    )
    execution = replace(study.execution, slippage_bps=study.execution.slippage_bps * 2)
    stressed = Study(pricey, execution).run(raw, {}).metrics.total_return

    say(f"{runs} random runs")
    sides = m.by_side
    longs = sides["long"].trades if "long" in sides else 0
    shorts = sides["short"].trades if "short" in sides else 0
    long_share = longs / (longs + shorts) if longs + shorts else 0.5
    chance = _chance(study, raw, max(1, m.trades), m.exposure)

    jobs = [(raw, seed * 100_003 + i, chance, long_share) for i in range(runs)]
    if workers and workers > 1:
        import multiprocessing
        from concurrent.futures import ProcessPoolExecutor

        with ProcessPoolExecutor(
            max_workers=workers,
            mp_context=multiprocessing.get_context("spawn"),
            initializer=_hold,
            initargs=(study,),
        ) as pool:
            drawn = list(pool.map(_random_return, jobs, chunksize=2))
    else:
        _hold(study)
        drawn = [_random_return(j) for j in jobs]

    beaten_by = sum(1 for r in drawn if r >= m.total_return)
    p_value = (beaten_by + 1) / (len(drawn) + 1)
    ordered = sorted(drawn)
    median = ordered[len(ordered) // 2] if ordered else 0.0
    random_trades = _trial(study, raw, seed, chance, long_share).metrics.trades

    checks = [
        Check(
            "Beats random entries",
            p_value <= LUCK_LEVEL,
            f"{(1 - p_value) * 100:.0f}% of {len(drawn)} random runs did worse",
        ),
        Check("Enough trades", m.trades >= MIN_TRADES, f"{m.trades} trades"),
        Check(
            "Profitable in both periods",
            all(p.total_return > 0 for p in periods),
            " · ".join(f"{p.label} {p.total_return * 100:+.0f}%" for p in periods),
        ),
        Check("Survives double costs", stressed > 0, f"{stressed * 100:+.0f}% with costs doubled"),
    ]
    passed = [c.passed for c in checks]
    verdict = "pass" if all(passed) else ("weak" if passed[0] else "fail")
    return Reality(
        verdict=verdict,
        checks=checks,
        trades=m.trades,
        total_return=m.total_return,
        p_value=p_value,
        random_runs=len(drawn),
        random_median=median,
        random_best=ordered[-1] if ordered else 0.0,
        random_trades=random_trades,
        periods=periods,
        stressed_return=stressed,
    )
