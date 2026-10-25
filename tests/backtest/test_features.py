"""The entry filter's inputs, and the one property they must have: no lookahead."""

from __future__ import annotations

import math
import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from backtest import features
from backtest.market import Side
from backtest.view import build, wind
from marketdata.models import Bar, Interval

START = datetime(2026, 1, 5, tzinfo=UTC)  # a Monday
M5 = Interval.M5


def _series(n: int, seed: int = 3) -> list[Bar]:
    rng = random.Random(seed)
    price = 100.0
    out: list[Bar] = []
    for i in range(n):
        o = price
        price *= 1 + rng.gauss(0, 0.002)
        out.append(
            Bar(
                ts=START + timedelta(seconds=M5.seconds * i),
                open=o,
                high=max(o, price) * (1 + abs(rng.gauss(0, 0.001))),
                low=min(o, price) * (1 - abs(rng.gauss(0, 0.001))),
                close=price,
                volume=10 + rng.random() * 5,
            )
        )
    return out


def _vector(bars: list[Bar], i: int, side: Side = Side.LONG) -> list[float]:
    view, cursors = build(bars, M5, features.context(M5))
    wind(view, cursors, i)
    return features.extract(view, side)


def _same(a: list[float], b: list[float]) -> bool:
    return len(a) == len(b) and all(
        (math.isnan(x) and math.isnan(y)) or x == y for x, y in zip(a, b, strict=True)
    )


@pytest.mark.parametrize("i", [150, 700, 1500, 2499, 3333])
def test_changing_every_later_bar_does_not_move_the_vector(i: int) -> None:
    """The property that makes the filter honest.

    Everything after bar `i` is replaced with different prices and volumes. The
    vector at `i` - including the readings from the hourly, four-hourly and daily
    frames, whose current bucket is built partly from those later bars - must be
    exactly what it was.
    """
    bars = _series(5000)
    altered = bars[: i + 1] + [
        replace(b, open=b.open * 1.7, high=b.high * 2.1, low=b.low * 0.4, close=b.close * 0.6,
                volume=b.volume * 9)
        for b in bars[i + 1 :]
    ]

    assert _same(_vector(bars, i), _vector(altered, i))


def test_the_vector_has_one_value_per_name() -> None:
    for base in (Interval.M5, Interval.M15, Interval.H1, Interval.H4):
        expected = len(features.BASE_NAMES) + 2 * len(features.higher(base))
        assert len(features.names(base)) == expected
    assert len(_vector(_series(3000), 2500)) == len(features.names(M5))


def test_higher_timeframes_are_the_next_ones_up_and_the_daily_is_always_read() -> None:
    assert features.higher(Interval.M5) == (Interval.H1, Interval.H4)
    assert features.higher(Interval.H1) == (Interval.H4, Interval.D1)
    assert features.higher(Interval.D1) == ()
    assert Interval.D1 in features.context(Interval.M5)
    assert features.context(Interval.D1) == ()


def test_readings_by_hand() -> None:
    bars = _series(3000)
    i = 2500
    vector = dict(zip(features.names(M5), _vector(bars, i, Side.SHORT), strict=True))

    assert vector["side"] == -1.0
    assert vector["ret_1"] == pytest.approx(bars[i].close / bars[i - 1].close - 1)
    assert vector["ret_20"] == pytest.approx(bars[i].close / bars[i - 20].close - 1)
    assert vector["hour"] == bars[i].ts.hour + 0.0 or vector["hour"] == (
        bars[i].ts + timedelta(seconds=M5.seconds)
    ).hour
    assert 0 <= vector["weekday"] <= 6
    assert -1 < vector["dist_ema_50"] < 1


def test_the_hour_is_the_hour_the_decision_was_made_at() -> None:
    """At the close of the 10:55 bar the decision is made at 11:00."""
    bars = _series(400)
    view, cursors = build(bars, M5, features.context(M5))
    wind(view, cursors, 131)
    vector = dict(zip(features.names(M5), features.extract(view, Side.LONG), strict=True))
    assert view.at is not None
    assert vector["hour"] == view.at.hour
    assert vector["weekday"] == view.at.weekday()


def test_what_is_not_yet_known_is_nan_not_zero() -> None:
    vector = dict(zip(features.names(M5), _vector(_series(400), 30), strict=True))

    assert not math.isnan(vector["ret_5"])
    assert math.isnan(vector["ret_60"])
    assert math.isnan(vector["dist_ema_200"])
    assert math.isnan(vector["rsi_14_1h"]), "the first hourly bar has not closed yet"
    assert math.isnan(vector["pivot_pos"]), "no daily bar has closed yet"
