"""Strategies built from parts, rather than written as code."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from backtest.market import Side
from backtest.models import Action
from backtest.rules import SpecRule
from backtest.spec import SpecError, condition, operand, parse
from backtest.view import build, wind
from marketdata.models import Bar, Interval

START = datetime(2026, 1, 1, tzinfo=UTC)


def _bars(closes: list[float], interval: Interval = Interval.M5) -> list[Bar]:
    """Bars at these closes, each with a range of one either side."""
    return [
        Bar(
            ts=START + timedelta(seconds=interval.seconds * i),
            open=c,
            high=c + 1,
            low=c - 1,
            close=c,
            volume=1.0,
        )
        for i, c in enumerate(closes)
    ]


def _at(bars: list[Bar], i: int, context: tuple[Interval, ...] = ()):  # type: ignore[no-untyped-def]
    view, cursors = build(bars, Interval.M5, context)
    wind(view, cursors, i)
    return view


# --------------------------------------------------------------------------
# Reading what a screen posts
# --------------------------------------------------------------------------


def test_a_bare_number_is_an_operand() -> None:
    """Writing {"kind": "value", "value": 30} for 30 would be silly."""
    assert operand(30).read(_at(_bars([1.0] * 5), 4)) == 30.0


def test_a_timeframe_is_worked_out_from_the_conditions() -> None:
    """Somebody picking an hourly EMA should not also have to declare the hour."""
    spec = parse(
        {
            "interval": "5m",
            "long_entry": {
                "left": {"kind": "indicator", "name": "ema", "length": 50, "tf": "1h"},
                "op": "above",
                "right": {"kind": "indicator", "name": "ema", "length": 200, "tf": "4h"},
            },
            "stop": {"kind": "percent", "value": 1.0},
        }
    )

    assert spec.context == (Interval.H4, Interval.H1)


def test_every_mistake_is_reported_at_once() -> None:
    """Somebody building this on a screen should see all of it in one pass."""
    with pytest.raises(SpecError) as raised:
        parse({"interval": "5m"})

    assert "Nothing to enter on" in str(raised.value)


def test_a_context_timeframe_below_the_traded_one_is_refused() -> None:
    with pytest.raises(SpecError, match="longer timeframe"):
        parse(
            {
                "interval": "1h",
                "long_entry": {
                    "left": {"kind": "price", "field": "close", "tf": "5m"},
                    "op": "above",
                    "right": 10,
                },
                "stop": {"kind": "percent", "value": 1.0},
            }
        )


def test_a_strategy_with_no_way_out_is_refused() -> None:
    """Without an exit, a stop or a target, a position ends when the data does."""
    with pytest.raises(SpecError, match="Nothing to exit on"):
        parse(
            {
                "long_entry": {
                    "left": {"kind": "price", "field": "close"}, "op": "above", "right": 10
                }
            }
        )


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"kind": "indicator", "name": "stochastic", "length": 9}, "no indicator called"),
        ({"kind": "price", "field": "wick"}, "a candle has no"),
        ({"kind": "pivot", "level": "R9"}, "not a pivot level"),
        ({"kind": "indicator", "name": "ema", "length": 0}, "period of at least 1"),
        ({"kind": "indicator", "name": "ema", "length": 5, "tf": "7m"}, "not a bar size"),
        ({"kind": "price", "field": "close", "ago": -1}, "cannot be negative"),
        ({"kind": "wishful"}, "not something that can be measured"),
    ],
)
def test_a_bad_operand_says_what_to_do_instead(raw: dict[str, object], message: str) -> None:
    """These messages are read by whoever built the strategy, not by a developer."""
    with pytest.raises(SpecError, match=message):
        operand(raw)


def test_a_bad_comparison_lists_the_ones_that_exist() -> None:
    with pytest.raises(SpecError, match="not a comparison"):
        condition({"left": 1, "op": "exceeds", "right": 2})


# --------------------------------------------------------------------------
# Evaluating
# --------------------------------------------------------------------------


def test_a_crossing_needs_two_bars() -> None:
    """Otherwise "crosses below" fires on every bar it is merely below, which is a
    different and usually much worse strategy."""
    crossing = condition(
        {
            "left": {"kind": "price", "field": "close"},
            "op": "crosses_below",
            "right": {"kind": "indicator", "name": "sma", "length": 3},
        }
    )
    # rises, then drops through the average, then stays below it
    bars = _bars([10.0, 11.0, 12.0, 13.0, 5.0, 4.0])

    fired = [i for i in range(len(bars)) if crossing.holds(_at(bars, i))]

    assert fired == [4]


def test_a_comparison_during_warm_up_is_false_rather_than_an_error() -> None:
    """A rule does not trade while it is still blind."""
    test = condition(
        {
            "left": {"kind": "indicator", "name": "ema", "length": 50},
            "op": "above",
            "right": 0,
        }
    )

    assert test.holds(_at(_bars([10.0] * 10), 9)) is False


def test_arithmetic_is_unknown_when_any_part_of_it_is() -> None:
    """"close minus two ATRs" during ATR's warm-up is not the close."""
    expression = operand(
        {
            "kind": "math",
            "op": "-",
            "left": {"kind": "price", "field": "close"},
            "right": {"kind": "indicator", "name": "atr", "length": 50},
        }
    )

    assert expression.read(_at(_bars([10.0] * 10), 9)) is None


def test_and_or_and_not() -> None:
    bars = _bars([10.0] * 5)
    view = _at(bars, 4)
    above = {"left": {"kind": "price", "field": "close"}, "op": "above", "right": 5}
    below = {"left": {"kind": "price", "field": "close"}, "op": "below", "right": 5}

    assert condition({"all": [above, above]}).holds(view) is True
    assert condition({"all": [above, below]}).holds(view) is False
    assert condition({"any": [above, below]}).holds(view) is True
    assert condition({"not": below}).holds(view) is True


def test_a_condition_reads_the_timeframe_it_names() -> None:
    """An hourly close is the hour's close, not the five-minute bar's."""
    bars = _bars([float(100 + i) for i in range(36)])
    hourly = operand({"kind": "price", "field": "close", "tf": "1h"})

    view = _at(bars, 20, (Interval.H1,))

    # bar 20 closes at 01:45, so the newest finished hour is the one that ended
    # at 01:00 - which closed on bar 11
    assert hourly.read(view) == bars[11].close


# --------------------------------------------------------------------------
# As a rule
# --------------------------------------------------------------------------


def test_the_previous_candles_low_becomes_the_stop() -> None:
    """The commonest stop there is, and the one you asked for by name."""
    spec = parse(
        {
            "long_entry": {
                "left": {"kind": "price", "field": "close"}, "op": "above", "right": 0
            },
            "stop": {"kind": "candle", "field": "low", "ago": 1},
            "target": {"kind": "reward", "value": 2.0},
        }
    )
    bars = _bars([10.0, 20.0, 30.0])

    intent = SpecRule(spec).entry(_at(bars, 2))

    assert intent.action is Action.ENTER
    assert intent.stop == bars[1].low  # 19.0
    # twice the risk: entry 30, stop 19, so 11 of risk and 22 of reward
    assert intent.target == pytest.approx(52.0)


def test_a_stop_on_the_wrong_side_of_the_entry_is_dropped_not_inverted() -> None:
    """The previous candle's low is not a stop for a short.

    Honouring it would close the position on the next tick; inverting it silently
    would be inventing a strategy nobody asked for.
    """
    spec = parse(
        {
            "short_entry": {
                "left": {"kind": "price", "field": "close"}, "op": "above", "right": 0
            },
            "stop": {"kind": "candle", "field": "low", "ago": 1},
        }
    )

    intent = SpecRule(spec).entry(_at(_bars([10.0, 20.0, 30.0]), 2))

    assert intent.action is Action.ENTER
    assert intent.side is Side.SHORT
    assert intent.stop is None


def test_a_strategy_that_wants_both_sides_at_once_stands_aside() -> None:
    """A contradiction in the strategy, kept visible rather than resolved."""
    always = {"left": {"kind": "price", "field": "close"}, "op": "above", "right": 0}
    spec = parse(
        {"long_entry": always, "short_entry": always, "stop": {"kind": "percent", "value": 1}}
    )

    intent = SpecRule(spec).entry(_at(_bars([10.0] * 4), 3))

    assert intent.action is Action.NOTHING


def test_the_reason_reads_as_the_sentence_that_was_built() -> None:
    """A trade log of reasons is readable; one of numbers is not."""
    spec = parse(
        {
            "long_entry": {
                "all": [
                    {
                        "left": {"kind": "price", "field": "close"},
                        "op": "crosses_below",
                        "right": {"kind": "indicator", "name": "ema", "length": 5},
                    },
                    {
                        "left": {"kind": "indicator", "name": "rsi", "length": 14},
                        "op": "below",
                        "right": 30,
                    },
                ]
            },
            "stop": {"kind": "percent", "value": 1.0},
        }
    )

    assert spec.long_entry is not None
    assert spec.long_entry.describe() == (
        "(close crosses below EMA 5 and RSI 14 below 30)"
    )


def test_an_exit_is_evaluated_per_side() -> None:
    """"Close the long" and "close the short" are usually different sentences."""
    spec = parse(
        {
            "long_entry": {"left": {"kind": "price", "field": "close"}, "op": "above",
                           "right": 0},
            "short_entry": {"left": {"kind": "price", "field": "close"}, "op": "below",
                            "right": 0},
            "long_exit": {"left": {"kind": "price", "field": "close"}, "op": "above",
                          "right": 5},
            "short_exit": {"left": {"kind": "price", "field": "close"}, "op": "below",
                           "right": 5},
        }
    )
    from backtest.models import Position

    view = _at(_bars([10.0] * 4), 3)
    rule = SpecRule(spec)
    long = Position(side=Side.LONG, quantity=1.0, entry=10.0, opened_at=START)
    short = Position(side=Side.SHORT, quantity=1.0, entry=10.0, opened_at=START)

    assert rule.exit(view, long).action is Action.EXIT
    assert rule.exit(view, short).action is Action.NOTHING


# --------------------------------------------------------------------------
# The newer indicators and "bars since"
# --------------------------------------------------------------------------


def _wave(n: int) -> list[float]:
    return [100.0 + (i % 17) * 1.5 - (i % 5) for i in range(n)]


def test_macd_takes_its_slow_period_and_defaults_to_the_usual_one() -> None:
    bars = _bars(_wave(120))
    view = _at(bars, 100)

    usual = operand({"kind": "indicator", "name": "macd", "length": 12})
    longer = operand({"kind": "indicator", "name": "macd", "length": 12, "slow": 40})

    assert usual.extra == 26 and longer.extra == 40
    assert usual.read(view) != longer.read(view)
    assert usual.read(view) == view.base.line("macd", 12, 26.0)


def test_macd_with_a_slow_period_that_is_not_slower_is_refused() -> None:
    with pytest.raises(SpecError, match="slow period"):
        operand({"kind": "indicator", "name": "macd", "length": 20, "slow": 20})


def test_a_bollinger_band_reads_from_its_own_width() -> None:
    bars = _bars(_wave(80))
    view = _at(bars, 60)
    wide = operand({"kind": "indicator", "name": "bb_upper", "length": 20, "mult": 3.0})
    narrow = operand({"kind": "indicator", "name": "bb_upper", "length": 20, "mult": 1.0})
    mid = operand({"kind": "indicator", "name": "sma", "length": 20}).read(view)

    assert mid is not None
    assert (wide.read(view) or 0) - mid == pytest.approx(3 * ((narrow.read(view) or 0) - mid))
    with pytest.raises(SpecError, match="more than zero"):
        operand({"kind": "indicator", "name": "bb_lower", "length": 20, "mult": 0})


def test_volume_is_something_a_bar_can_be_measured_by_and_a_trigger_cannot_rest_at() -> None:
    bars = _bars([100.0] * 10)
    assert operand({"kind": "price", "field": "volume"}).read(_at(bars, 5)) == 1.0
    with pytest.raises(SpecError, match="candle has no"):
        parse(
            {
                "long_entry": {"left": 1, "op": "above", "right": 0},
                "stop": {"kind": "percent", "value": 1},
                "trigger": {"kind": "break", "field": "volume"},
            }
        )


def test_the_newer_indicators_are_not_offered_to_a_chart_that_cannot_draw_them() -> None:
    spec = parse(
        {
            "long_entry": {
                "left": {"kind": "indicator", "name": "macd", "length": 12},
                "op": "above",
                "right": {"kind": "indicator", "name": "ema", "length": 20},
            },
            "stop": {"kind": "percent", "value": 1},
        }
    )
    assert spec.indicators() == [("ema", 20, None)]


def _volume_is(value: float) -> dict[str, object]:
    return {"left": {"kind": "price", "field": "volume"}, "op": "equals", "right": value}


def _ramp_volume(n: int) -> list[Bar]:
    return [
        Bar(
            ts=START + timedelta(minutes=5 * i), open=100, high=101, low=99, close=100, volume=i
        )
        for i in range(n)
    ]


def test_bars_since_counts_closed_bars_back_to_the_last_time_it_held() -> None:
    bars = _ramp_volume(30)
    since = operand({"kind": "since", "when": _volume_is(5), "max": 20})

    assert since.read(_at(bars, 5)) == 0.0
    assert since.read(_at(bars, 8)) == 3.0
    assert since.read(_at(bars, 20)) == 15.0
    # Before it ever held there is no answer, and not a zero.
    assert since.read(_at(bars, 4)) is None


def test_bars_since_gives_up_past_its_reach() -> None:
    bars = _ramp_volume(30)
    since = operand({"kind": "since", "when": _volume_is(5), "max": 5})
    assert since.read(_at(bars, 10)) == 5.0
    assert since.read(_at(bars, 11)) is None


def test_bars_since_read_a_bar_back_is_the_answer_a_bar_ago() -> None:
    """A crossing re-reads each side one bar back, so this has to shift cleanly."""
    bars = _ramp_volume(30)
    now = operand({"kind": "since", "when": _volume_is(5), "max": 20})
    before = operand({"kind": "since", "when": _volume_is(5), "max": 20, "ago": 1})

    assert before.read(_at(bars, 9)) == now.read(_at(bars, 8)) == 3.0


def test_bars_since_cannot_see_bars_that_have_not_closed() -> None:
    quiet = _ramp_volume(30)
    loud = [*quiet[:10], *[b.__class__(**{**b.__dict__, "volume": 5.0}) for b in quiet[10:]]]
    since = operand({"kind": "since", "when": _volume_is(5), "max": 20})

    assert since.read(_at(quiet, 9)) == since.read(_at(loud, 9)) == 4.0


def test_bars_since_must_stay_on_the_traded_timeframe_and_cannot_nest() -> None:
    hourly = {"left": {"kind": "indicator", "name": "ema", "length": 5, "tf": "1h"},
              "op": "above", "right": 1}
    with pytest.raises(SpecError, match="timeframe being traded"):
        operand({"kind": "since", "when": hourly})
    inner = {"left": {"kind": "since", "when": _volume_is(5)}, "op": "below", "right": 3}
    with pytest.raises(SpecError, match="another bars since"):
        operand({"kind": "since", "when": inner})
    with pytest.raises(SpecError, match="between 1 and 1000"):
        operand({"kind": "since", "when": _volume_is(5), "max": 0})


def test_a_rule_can_wait_for_a_pullback_after_a_breakout() -> None:
    """The reason the operand exists: 'within three bars of the breakout'."""
    breakout = {"left": {"kind": "price", "field": "close"}, "op": "above",
                "right": {"kind": "indicator", "name": "highest", "length": 10, "ago": 1}}
    spec = parse(
        {
            "long_entry": {
                "all": [
                    {"left": {"kind": "since", "when": breakout, "max": 5}, "op": "below",
                     "right": 3},
                    {"left": {"kind": "price", "field": "close"}, "op": "below",
                     "right": {"kind": "price", "field": "close", "ago": 1}},
                ]
            },
            "stop": {"kind": "percent", "value": 1},
        }
    )
    closes = [100.0] * 15 + [110.0, 112.0, 111.0, 110.5, 110.0, 109.0, 108.0, 107.0]
    rule = SpecRule(spec)

    bars = _bars(closes)
    taken = [i for i in range(len(closes)) if rule.entry(_at(bars, i)).action is Action.ENTER]

    # The last breakout is bar 16 (112 clears 15's high). Bars 17 and 18 are one and
    # two bars after it and close lower; 19 is three after, outside "below 3".
    assert taken == [17, 18]
