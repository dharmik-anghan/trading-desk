"""Parameters in a spec: binding, ranges and constraints."""

from __future__ import annotations

from typing import Any

import pytest

from backtest.params import (
    ParamError,
    bind,
    declared,
    defaults,
    feasible,
    placeholders,
    violations,
)
from backtest.spec import SpecError, parse


def _ema(n: Any) -> dict[str, Any]:
    return {"kind": "indicator", "name": "ema", "length": n}


def _document(fast: Any, slow: Any, stop: Any) -> dict[str, Any]:
    return {
        "name": "cross",
        "long_entry": {"op": "crosses_above", "left": _ema(fast), "right": _ema(slow)},
        "long_exit": {"op": "crosses_below", "left": _ema(fast), "right": _ema(slow)},
        "stop": {"kind": "percent", "value": stop},
    }


PARAMETRISED: dict[str, Any] = {
    **_document({"param": "fast"}, {"param": "slow"}, {"param": "stop"}),
    "params": {
        "fast": {"type": "int", "low": 5, "high": 50, "step": 1, "default": 20},
        "slow": {"type": "int", "low": 20, "high": 200, "default": 50},
        "stop": {"type": "float", "low": 0.5, "high": 5.0, "default": 2.0},
    },
    "constraints": [{"lt": ["fast", "slow"]}],
}


def test_a_bound_spec_parses_exactly_like_one_written_by_hand() -> None:
    bound = bind(PARAMETRISED, {"fast": 12, "slow": 48, "stop": 1.5})

    by_hand = parse(_document(12, 48, 1.5))

    assert parse(bound) == by_hand


def test_binding_removes_the_search_description_from_the_document() -> None:
    bound = bind(PARAMETRISED, {"fast": 12, "slow": 48, "stop": 1.5})
    assert "params" not in bound and "constraints" not in bound
    assert placeholders(bound) == set()


def test_a_document_without_parameters_binds_to_itself() -> None:
    plain = _document(12, 48, 1.5)
    assert bind(plain, {}) == plain


def test_the_defaults_are_the_strategy_before_any_search() -> None:
    assert defaults(PARAMETRISED) == {"fast": 20, "slow": 50, "stop": 2.0}
    assert parse(bind(PARAMETRISED, defaults(PARAMETRISED))) == parse(_document(20, 50, 2.0))


def test_binding_does_not_touch_the_original() -> None:
    before = repr(PARAMETRISED)
    bind(PARAMETRISED, defaults(PARAMETRISED))
    assert repr(PARAMETRISED) == before


def test_an_unbound_document_cannot_be_run_by_accident() -> None:
    with pytest.raises(SpecError, match="fast, slow, stop still has to be given a value"):
        parse(PARAMETRISED)


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"fast": 4, "slow": 50, "stop": 1.0}, "outside 5 to 50"),
        ({"fast": 5.5, "slow": 50, "stop": 1.0}, "whole number"),
        ({"fast": 10, "slow": 50}, "no value for stop"),
        ({"fast": 10, "slow": 50, "stop": 1.0, "extra": 1}, "not a declared parameter"),
        ({"fast": "ten", "slow": 50, "stop": 1.0}, "has to be a number"),
        ({"fast": True, "slow": 50, "stop": 1.0}, "has to be a number"),
        ({"fast": 10, "slow": 50, "stop": float("nan")}, "outside"),
    ],
)
def test_values_outside_what_was_declared_are_refused(
    values: dict[str, Any], message: str
) -> None:
    with pytest.raises(ParamError, match=message):
        bind(PARAMETRISED, values)


def test_a_placeholder_nobody_declared_is_an_error() -> None:
    raw = {**_document({"param": "ghost"}, 20, 1.0)}
    with pytest.raises(ParamError, match="ghost used but not declared"):
        bind(raw, {})


def test_choices_can_be_substituted_too() -> None:
    raw = {
        **_document(10, 30, 1.0),
        "params": {"tf": {"type": "choice", "choices": ["5m", "15m"], "default": "15m"}},
    }
    raw["long_entry"]["left"]["tf"] = {"param": "tf"}
    bound = bind(raw, {"tf": "5m"})
    assert bound["long_entry"]["left"]["tf"] == "5m"
    with pytest.raises(ParamError, match="pick one of"):
        bind(raw, {"tf": "1h"})


@pytest.mark.parametrize(
    "block",
    [
        {"x": {"type": "int", "low": 5, "high": 5}},
        {"x": {"type": "int", "low": 1.5, "high": 5}},
        {"x": {"type": "float", "low": 0, "high": 1, "step": -1}},
        {"x": {"type": "float", "low": 0, "high": 1, "log": True}},
        {"x": {"type": "int", "low": 1, "high": 5, "default": 9}},
        {"x": {"type": "choice", "choices": []}},
        {"x": {"type": "string"}},
        {"x": {"type": "int", "high": 5}},
    ],
)
def test_a_malformed_declaration_is_refused(block: dict[str, Any]) -> None:
    with pytest.raises(ParamError):
        declared({"params": block})


def test_constraints_prune_before_anything_is_run() -> None:
    assert feasible(PARAMETRISED, {"fast": 10, "slow": 50, "stop": 1.0})
    assert violations(PARAMETRISED, {"fast": 50, "slow": 50, "stop": 1.0}) == ["fast lt slow"]
    assert not feasible(PARAMETRISED, {"fast": 60 - 10, "slow": 40, "stop": 1.0})


def test_constraints_can_compare_with_a_number() -> None:
    raw = {**PARAMETRISED, "constraints": [{"ge": ["slow", 30]}, {"ne": ["fast", "slow"]}]}
    assert feasible(raw, {"fast": 10, "slow": 30, "stop": 1.0})
    assert violations(raw, {"fast": 10, "slow": 25, "stop": 1.0}) == ["slow ge 30"]


@pytest.mark.parametrize(
    "constraints",
    [[{"lt": ["fast"]}], [{"near": ["fast", "slow"]}], [{"lt": ["fast", "nope"]}], ["fast<slow"]],
)
def test_a_malformed_constraint_is_refused(constraints: list[Any]) -> None:
    with pytest.raises(ParamError):
        violations({**PARAMETRISED, "constraints": constraints}, defaults(PARAMETRISED))


def test_a_neighbour_is_one_notch_away_and_stays_inside_the_range() -> None:
    params = declared(PARAMETRISED)
    fast, stop = params["fast"], params["stop"]

    # A twentieth of a 45-wide range is 2.25, which an int rounds to 2.
    assert fast.nudge(20, +1) == 22 and fast.nudge(20, -1) == 18
    assert fast.nudge(50, +1) is None
    assert fast.nudge(5, -1) is None
    assert stop.nudge(2.0, +1) == pytest.approx(2.0 + 4.5 / 20)
    choice = declared({"params": {"c": {"type": "choice", "choices": [1, 2]}}})["c"]
    assert choice.nudge(1, 1) is None
