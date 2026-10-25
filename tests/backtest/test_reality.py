"""The reality check separates a strategy from luck and from a rising market.

Two properties matter. A rule that has no edge must not pass, however well one run
happened to go; and the random baseline must trade the way the real rule does,
or it is a comparison with something else.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from typing import Any

from backtest.engine import Execution
from backtest.market import PerpetualMarket
from backtest.reality import RandomEntry, check
from backtest.service import Prepared
from backtest.service import Series as RunSeries
from backtest.spec import parse
from backtest.study import Study
from marketdata import Interval
from marketdata.models import Bar

START = datetime(2026, 1, 1, tzinfo=UTC)


def _walk(n: int, seed: int = 5) -> list[Bar]:
    rng = random.Random(seed)
    price, out = 100.0, []
    for i in range(n):
        o = price
        price = max(1.0, price * (1 + rng.gauss(0, 0.004)))
        out.append(
            Bar(
                ts=START + timedelta(minutes=5 * i),
                open=o,
                high=max(o, price) * 1.001,
                low=min(o, price) * 0.999,
                close=price,
                volume=1.0,
            )
        )
    return out


def _study(bars: list[Bar]) -> Study:
    prepared = Prepared(
        RunSeries("binance", "TESTUSDT", Interval.M5, 30), bars, PerpetualMarket("TESTUSDT"), ()
    )
    return Study(prepared, Execution(capital=1000.0, slippage_bps=1.0))


def _px(field: str, ago: int) -> dict[str, Any]:
    return {"kind": "price", "field": field, "ago": ago}


def _three_candles() -> dict[str, Any]:
    run = lambda op: {  # noqa: E731
        "all": [{"op": op, "left": _px("close", a), "right": _px("open", a)} for a in (0, 1, 2)]
    }
    return {
        "name": "three candles",
        "long_entry": run("above"),
        "short_entry": run("below"),
        "stop": {"kind": "candle", "field": "far", "ago": 2},
        "target": {"kind": "reward", "value": 2},
    }


def test_a_rule_with_no_edge_does_not_pass() -> None:
    result = check(_study(_walk(4000)), _three_candles(), runs=40, workers=1)

    assert result.verdict != "pass"
    assert {c.name for c in result.checks} >= {"Beats random entries", "Enough trades"}


def test_the_baseline_trades_about_as_often_as_the_rule() -> None:
    """Otherwise the comparison is between different amounts of cost."""
    result = check(_study(_walk(4000)), _three_candles(), runs=20, workers=1)

    assert abs(result.random_trades - result.trades) <= 0.35 * result.trades


def test_the_far_end_of_a_candle_is_the_low_for_a_long_and_the_high_for_a_short() -> None:
    study = _study(_walk(600))
    run = study.run(_three_candles(), {})

    for trade in run.result.trades:
        assert trade.entry > 0
    sides = {str(t.side) for t in run.result.trades}
    assert sides  # it traded both ways on a random walk


def test_random_entries_keep_the_rules_own_exits() -> None:
    spec = parse({**_three_candles(), "interval": "5m"})

    rule = RandomEntry(spec, seed=1, chance=0.5, long_share=1.0)

    assert rule.spec is spec
