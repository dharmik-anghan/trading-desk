"""Indicator conditions: candles built only from finished minutes, and indicator
values that match the whole-series functions in `analytics/indicators.py`."""

from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta

import pytest

from analytics import indicators as ind
from optbt.market import Bar
from optbt.signals import Condition, Operand, Signals, _Frame

DAY = date(2026, 9, 21)


def _walk(n: int, seed: int = 3) -> list[Bar]:
    rng = random.Random(seed)
    price = 23450.0
    out = []
    start = datetime.combine(DAY, time(9, 15))
    for i in range(n):
        o = price
        price = price * (1 + rng.gauss(0, 0.0008))
        hi = max(o, price) * (1 + abs(rng.gauss(0, 0.0003)))
        lo = min(o, price) * (1 - abs(rng.gauss(0, 0.0003)))
        out.append(Bar(start + timedelta(minutes=i), o, hi, lo, price, 0))
    return out


def _no_context():  # type: ignore[no-untyped-def]
    raise AssertionError("no level was asked for")


@pytest.mark.parametrize(
    ("op", "expected"),
    [
        (Operand("ema", length=20), lambda bars: ind.ema(ind.closes(bars), 20)),
        (Operand("sma", length=20), lambda bars: ind.sma(ind.closes(bars), 20)),
        (Operand("rsi", length=14), lambda bars: ind.rsi(ind.closes(bars), 14)),
        (Operand("supertrend", length=10, mult=3.0), lambda bars: ind.supertrend(bars, 10, 3.0)),
    ],
    ids=["ema", "sma", "rsi", "supertrend"],
)
def test_one_candle_at_a_time_gives_the_whole_series_value(op, expected) -> None:  # type: ignore[no-untyped-def]
    bars = _walk(300)
    frame = _Frame(1, [op])
    got = []
    for bar in bars:
        frame.add(bar, False, _no_context)
        got.append(frame.cur[op.key])
    want = expected(bars)
    assert [g is None for g in got] == [w is None for w in want]
    assert got == want  # the same arithmetic, so the same floats


def test_a_five_minute_candle_is_read_only_once_its_last_minute_has_closed() -> None:
    """09:15-09:19 is one candle. Its close is the 09:19 bar's, and it is known
    from that bar on - not at 09:18, while the candle is still forming."""
    bars = _walk(10)
    frame = _Frame(5, [Operand("price")])
    seen = {}
    for bar in bars:
        frame.add(bar, False, _no_context)
        seen[bar.ts.time()] = frame.cur.get(("price",))
    assert seen[time(9, 18)] is None
    assert seen[time(9, 19)] == bars[4].close
    assert seen[time(9, 23)] == bars[4].close  # the next candle is still forming
    assert seen[time(9, 24)] == bars[9].close


def test_a_short_last_candle_is_finished_with_the_session() -> None:
    """An hour chart's 15:15 candle has only 15 minutes; it closes with the day."""
    bars = _walk(375)
    frame = _Frame(60, [Operand("price")])
    for i, bar in enumerate(bars):
        frame.add(bar, i == len(bars) - 1, _no_context)
    assert frame.cur[("price",)] == bars[-1].close
    assert frame.completed_at == bars[-1].ts


def _signals_after(closes: list[float], *conditions: Condition) -> Signals:
    signals = Signals(conditions)
    start = datetime.combine(DAY, time(9, 15))
    for i, c in enumerate(closes):
        bar = Bar(start + timedelta(minutes=i), c, c, c, c, 0)
        for frame in signals._frames.values():
            frame.add(bar, False, _no_context)
    return signals


SPOT = Operand("price")


def test_above_and_below_compare_the_last_candle() -> None:
    above = Condition(SPOT, "above", Operand("number", value=100), timeframe=1)
    below = Condition(SPOT, "below", Operand("number", value=100), timeframe=1)
    s = _signals_after([99, 101], above, below)
    assert s.check(above) is True
    assert s.check(below) is False


def test_a_cross_holds_only_on_the_candle_it_happened() -> None:
    cross = Condition(SPOT, "crosses_above", Operand("number", value=100), timeframe=1)
    assert _signals_after([99, 101], cross).check(cross) is True
    assert _signals_after([99, 101, 102], cross).check(cross) is False
    assert _signals_after([101], cross).check(cross) is None  # nothing before it


def test_a_cross_before_a_time_does_not_count_after_it() -> None:
    cross = Condition(SPOT, "crosses_above", Operand("number", value=100), timeframe=1)
    s = _signals_after([99, 101], cross)
    assert s.check(cross, since=datetime.combine(DAY, time(9, 16))) is True
    assert s.check(cross, since=datetime.combine(DAY, time(9, 17))) is False


def test_an_indicator_still_warming_up_is_unknown() -> None:
    c = Condition(SPOT, "above", Operand("ema", length=5), timeframe=1)
    assert _signals_after([100, 101, 102], c).check(c) is None
    assert _signals_after([100, 101, 102, 103, 104, 105], c).check(c) is True


def test_all_and_any() -> None:
    yes = Condition(SPOT, "above", Operand("number", value=50), timeframe=1)
    no = Condition(SPOT, "below", Operand("number", value=50), timeframe=1)
    unknown = Condition(SPOT, "above", Operand("ema", length=50), timeframe=1)
    s = _signals_after([100, 100], yes, no, unknown)
    assert s.met([yes, no], "all") is False
    assert s.met([yes, no], "any") is True
    assert s.met([yes, unknown], "all") is None
    assert s.met([no, unknown], "all") is False
    assert s.met([yes, unknown], "any") is True
    assert s.met([no, unknown], "any") is None


def test_warm_up_covers_an_emas_seed_on_its_own_timeframe() -> None:
    """EMA 20 on 15m wants 101 candles: 1,515 minutes, five sessions, plus one."""
    s = Signals([Condition(SPOT, "above", Operand("ema", length=20), timeframe=15)])
    assert s.warmup_sessions() == 6


def test_the_readout_names_the_numbers() -> None:
    c = Condition(SPOT, "below", Operand("number", value=23440), timeframe=1)
    s = _signals_after([23430.5], c)
    assert s.readout(c) == "spot 23,430.50 below 23440 on 1m"
