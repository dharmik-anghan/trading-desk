"""A legs strategy survives being written down: to JSON and back, unchanged."""

from __future__ import annotations

import json
from datetime import time

import pytest

from api.routers.optbt import RunRequest
from optbt.data.models import Kind
from optbt.engine import Level, Side
from optbt.signals import Condition, EntrySignal, ExitSignal, Operand
from optbt.spec import VERSION, from_dict, to_dict
from optbt.strategies.legs import (
    Adjustment,
    DayFilter,
    ExpiryChoice,
    LegsConfig,
    LegSpec,
    StrikeRule,
    iron_condor,
    straddle,
)

QUARTER = Level("pct", 0.25)

CONFIGS = {
    "defaults": LegsConfig(legs=straddle()),
    "intraday straddle": LegsConfig(
        legs=(
            LegSpec(Side.SELL, Kind.CALL, stop=QUARTER),
            LegSpec(Side.SELL, Kind.PUT, stop=QUARTER),
        ),
        trail_to_cost=True,
    ),
    "adjusted delta condor": LegsConfig(
        legs=(
            LegSpec(Side.SELL, Kind.CALL, strike=StrikeRule("delta", delta=0.3)),
            LegSpec(Side.BUY, Kind.CALL, strike=StrikeRule("delta", delta=0.17)),
            LegSpec(Side.SELL, Kind.PUT, strike=StrikeRule("delta", delta=0.3)),
            LegSpec(Side.BUY, Kind.PUT, strike=StrikeRule("delta", delta=0.17)),
        ),
        expiry=ExpiryChoice("days", min_left=3),
        entry=time(10, 0),
        hold="expiry",
        weekdays=frozenset({0}),
        target_credit=0.5,
        stop_credit=1.0,
        exit_dte=21,
        equal_wings=True,
        adjust=Adjustment(enabled=True, fall_from="short", rise_points=-100, max_per_trade=2),
        days=DayFilter(vix_pct_max=80, vix_lookback=126),
    ),
    "filtered strangle": LegsConfig(
        legs=(
            LegSpec(
                Side.SELL,
                Kind.CALL,
                lots=2,
                expiry=ExpiryChoice(nth=2),
                strike=StrikeRule("pct", pct=2.0),
                target=Level("points", 20),
            ),
            LegSpec(
                Side.SELL,
                Kind.PUT,
                expiry=ExpiryChoice(nth=2),
                strike=StrikeRule("premium", premium=60),
            ),
        ),
        entry=time(9, 45, 30),
        mtm_stop=8000,
        mtm_target=4000,
        days=DayFilter(
            expiry_day="skip",
            gap_min=-0.5,
            gap_max=0.5,
            open_zones=frozenset({"S1-P", "P-R1"}),
            dte_min=5,
        ),
    ),
    "condor preset": LegsConfig(legs=iron_condor(), hold="expiry"),
    "straddle on signals": LegsConfig(
        legs=straddle(),
        entry_signal=EntrySignal(
            mode="wait",
            join="any",
            conditions=(
                Condition(Operand("price"), "crosses_below", Operand("ema", length=20), 5),
                Condition(Operand("rsi", length=14), "above", Operand("number", value=60), 15),
            ),
        ),
        exit_signal=ExitSignal(
            conditions=(
                Condition(Operand("price"), "above", Operand("level", level="R1"), 1),
                Condition(
                    Operand("price"), "below", Operand("supertrend", length=10, mult=2.5), 3
                ),
            ),
        ),
    ),
}


@pytest.mark.parametrize("name", CONFIGS)
def test_a_config_round_trips_through_json_unchanged(name: str) -> None:
    config = CONFIGS[name]
    written = json.dumps(to_dict(config))
    assert from_dict(json.loads(written)) == config


def test_a_spec_says_which_version_of_the_shape_it_is() -> None:
    assert to_dict(CONFIGS["defaults"])["version"] == VERSION


def test_a_spec_from_a_newer_writer_is_refused_rather_than_misread() -> None:
    raw = to_dict(CONFIGS["defaults"]) | {"version": VERSION + 1}
    with pytest.raises(ValueError, match="newer"):
        from_dict(raw)


def test_what_is_left_out_takes_the_defaults() -> None:
    assert from_dict({"legs": [{"side": "sell", "kind": "CE"}]}) == LegsConfig(
        legs=(LegSpec(Side.SELL, Kind.CALL),)
    )


def test_an_api_request_is_read_as_the_same_spec() -> None:
    """The request's JSON is the spec's shape, so the API cannot drift from it."""
    config = CONFIGS["filtered strangle"]
    body = {k: v for k, v in to_dict(config).items() if k != "version"}
    body |= {"start": "2025-01-01", "end": "2025-06-30"}
    assert RunRequest.model_validate_json(json.dumps(body)).config() == config


def test_a_version_1_spec_reads_as_the_choice_it_meant() -> None:
    """Version 1 named each leg's expiry; each name is one choice now."""
    raw = {
        "version": 1,
        "legs": [
            {"side": "sell", "kind": "CE", "expiry": "next_week"},
            {"side": "buy", "kind": "CE", "expiry": "days", "expiry_days": 30},
            {"side": "sell", "kind": "PE"},
        ],
    }
    legs = from_dict(raw).legs
    assert [leg.expiry for leg in legs] == [
        ExpiryChoice("weekly", nth=2),
        ExpiryChoice("days", days=30),
        None,
    ]


def test_an_api_request_in_the_first_format_still_runs_as_it_meant() -> None:
    """A page loaded before the expiry choice sends each leg's expiry by name."""
    body = {
        "start": "2025-01-01",
        "end": "2025-06-30",
        "legs": [
            {"side": "sell", "kind": "CE", "expiry": "days", "expiry_days": 45},
            {"side": "buy", "kind": "PE", "expiry": "next_week"},
        ],
    }
    legs = RunRequest.model_validate_json(json.dumps(body)).config().legs
    assert [leg.expiry for leg in legs] == [
        ExpiryChoice("days", days=45),
        ExpiryChoice("weekly", nth=2),
    ]
