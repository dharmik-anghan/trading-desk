"""Numbers in a strategy that are to be searched rather than typed.

A strategy is data, so a search over it is a search over some of its numbers. Any
number in the tree can be written as a placeholder, and a `params` block says what
each one may be:

    {"params": {"fast": {"type": "int", "low": 5, "high": 50, "default": 20},
                "slow": {"type": "int", "low": 20, "high": 200, "default": 50}},
     "constraints": [{"lt": ["fast", "slow"]}],
     "long_entry": {"op": "crosses_above",
                    "left":  {"kind": "indicator", "name": "ema", "length": {"param": "fast"}},
                    "right": {"kind": "indicator", "name": "ema", "length": {"param": "slow"}}}}

`bind` swaps the placeholders for chosen values and hands back the plain document
`spec.parse` has always read, so a bound strategy is validated by exactly the
code that validates one typed on a screen, and a trial is nothing more than
`(document, values)` - small enough to send to another process and to write to a
log, which is what makes a result reproducible.

`default` is the value a person would have typed: the strategy before any search.
It is what "hand-tuned" means when a search is compared with what it started from,
which is why it lives beside the range rather than in a second document.

Nothing here knows how a search chooses values. That is `backtest/walkforward.py`,
and keeping the two apart is what lets a spec be stored and shared without
dragging an optimiser along.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: The keys that describe a search and are not part of the strategy.
RESERVED = ("params", "constraints")

_KINDS = ("int", "float", "choice")
_RELATIONS = {
    "lt": lambda a, b: a < b,
    "le": lambda a, b: a <= b,
    "gt": lambda a, b: a > b,
    "ge": lambda a, b: a >= b,
    "ne": lambda a, b: a != b,
}


class ParamError(ValueError):
    """A parameter block or a set of values that cannot be used, said plainly."""


@dataclass(frozen=True)
class Param:
    """One searchable number, or one choice among a list."""

    name: str
    kind: str
    low: float = 0.0
    high: float = 0.0
    #: The grid the value lies on. None means any value in the range.
    step: float | None = None
    choices: tuple[Any, ...] = ()
    #: What the strategy used before any search. Always inside the range.
    default: Any = None
    #: Sample on a logarithmic scale, for a range that spans orders of magnitude.
    log: bool = False

    def check(self, value: Any) -> Any:
        """The value, if it is one this parameter may take; otherwise an error."""
        if self.kind == "choice":
            if value not in self.choices:
                raise ParamError(f"{self.name} is {value!r}; pick one of {list(self.choices)}")
            return value
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ParamError(f"{self.name} has to be a number, not {value!r}")
        if not math.isfinite(value) or not self.low <= value <= self.high:
            raise ParamError(f"{self.name} is {value}, outside {self.low:g} to {self.high:g}")
        if self.kind == "int":
            if value != int(value):
                raise ParamError(f"{self.name} has to be a whole number, not {value}")
            return int(value)
        return float(value)

    def nudge(self, value: Any, direction: int) -> Any | None:
        """A neighbour of `value`, one notch up (+1) or down (-1), or None at the edge.

        A notch is the grid step when there is one and otherwise a twentieth of the
        range - near enough that a robust setting should score about the same,
        far enough to move off a single-point peak. Used to ask whether a good
        value is on a plateau or on a spike.
        """
        if self.kind == "choice":
            return None
        span = self.high - self.low
        notch = max(self.step or 0.0, span / 20.0)
        if self.kind == "int":
            notch = max(1.0, round(notch))
        moved = min(self.high, max(self.low, value + direction * notch))
        if self.kind == "int":
            moved = int(round(moved))
        return None if moved == value else moved


def declared(raw: Mapping[str, Any]) -> dict[str, Param]:
    """The parameters a document declares, validated. Empty when it declares none."""
    block = raw.get("params")
    if block is None:
        return {}
    if not isinstance(block, dict):
        raise ParamError("params is an object of name: {type, low, high, ...}")
    return {str(name): _param(str(name), spec) for name, spec in block.items()}


def _param(name: str, spec: Any) -> Param:
    if not isinstance(spec, dict):
        raise ParamError(f"{name} has to be an object with a type")
    kind = str(spec.get("type", "")).lower()
    if kind not in _KINDS:
        raise ParamError(f"{name} has type {kind!r}; pick one of {', '.join(_KINDS)}")

    if kind == "choice":
        choices = spec.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ParamError(f"{name} is a choice and needs a non-empty list of choices")
        default = spec.get("default", choices[0])
        param = Param(name, kind, choices=tuple(choices), default=default)
        param.check(default)
        return param

    try:
        low, high = float(spec["low"]), float(spec["high"])
    except (KeyError, TypeError, ValueError):
        raise ParamError(f"{name} needs a low and a high") from None
    if not low < high:
        raise ParamError(f"{name}: low ({low:g}) has to be below high ({high:g})")
    step = spec.get("step")
    if step is not None:
        step = float(step)
        if step <= 0:
            raise ParamError(f"{name}: step has to be positive")
    if kind == "int" and (low != int(low) or high != int(high) or (step and step != int(step))):
        raise ParamError(f"{name} is an int, so its low, high and step have to be whole")
    log = bool(spec.get("log"))
    if log and (low <= 0 or step):
        raise ParamError(f"{name}: a log scale needs a positive low and no step")
    default = spec.get("default", low + (high - low) / 2)
    if kind == "int":
        default = int(round(default))
    param = Param(name, kind, low, high, step, default=default, log=log)
    param.check(default)
    return param


def placeholders(node: Any) -> set[str]:
    """Every parameter name used anywhere in a document."""
    found: set[str] = set()
    if _is_placeholder(node):
        found.add(str(node["param"]))
    elif isinstance(node, dict):
        for key, value in node.items():
            if key not in RESERVED:
                found |= placeholders(value)
    elif isinstance(node, list):
        for value in node:
            found |= placeholders(value)
    return found


def defaults(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Each parameter's declared default: the strategy as it was before a search."""
    return {name: p.default for name, p in declared(raw).items()}


def bind(raw: Mapping[str, Any], values: Mapping[str, Any]) -> dict[str, Any]:
    """The document with every placeholder replaced by its value.

    Raises `ParamError` for a value outside its range, a missing or surplus value,
    or a placeholder nobody declared. A document with no `params` block binds to
    itself, so callers do not need to care whether a spec is parametrised.
    """
    params = declared(raw)
    used = placeholders(raw)
    unknown = used - params.keys()
    if unknown:
        raise ParamError(f"{', '.join(sorted(unknown))} used but not declared in params")
    missing = params.keys() - values.keys()
    if missing:
        raise ParamError(f"no value for {', '.join(sorted(missing))}")
    surplus = values.keys() - params.keys()
    if surplus:
        raise ParamError(f"{', '.join(sorted(surplus))} is not a declared parameter")
    checked = {name: params[name].check(value) for name, value in values.items()}
    return {
        key: _substitute(value, checked) for key, value in raw.items() if key not in RESERVED
    }


def _substitute(node: Any, values: Mapping[str, Any]) -> Any:
    if _is_placeholder(node):
        return values[str(node["param"])]
    if isinstance(node, dict):
        return {key: _substitute(value, values) for key, value in node.items()}
    if isinstance(node, list):
        return [_substitute(value, values) for value in node]
    return node


def _is_placeholder(node: Any) -> bool:
    return isinstance(node, dict) and set(node) == {"param"}


def violations(raw: Mapping[str, Any], values: Mapping[str, Any]) -> list[str]:
    """The constraints these values break, in words. Empty when they are all met.

    Checked before a trial is run rather than after: `fast < slow` is a statement
    about the search space, and a run spent finding out that fast was not below
    slow is a run spent learning nothing.
    """
    block = raw.get("constraints") or []
    if not isinstance(block, list):
        raise ParamError("constraints is a list")
    broken: list[str] = []
    names = declared(raw).keys()
    for rule in block:
        if not isinstance(rule, dict) or len(rule) != 1:
            raise ParamError(f"{rule!r} is not a constraint like {{'lt': ['fast', 'slow']}}")
        ((relation, pair),) = rule.items()
        if relation not in _RELATIONS or not isinstance(pair, list) or len(pair) != 2:
            raise ParamError(
                f"{rule!r} is not a constraint; use {', '.join(_RELATIONS)} with two operands"
            )
        a, b = (_operand(x, names, values) for x in pair)
        if not _RELATIONS[relation](a, b):
            broken.append(f"{pair[0]} {relation} {pair[1]}")
    return broken


def feasible(raw: Mapping[str, Any], values: Mapping[str, Any]) -> bool:
    return not violations(raw, values)


def _operand(item: Any, names: Any, values: Mapping[str, Any]) -> float:
    if isinstance(item, str):
        if item not in names:
            raise ParamError(f"constraint names {item!r}, which is not a declared parameter")
        return float(values[item])
    if isinstance(item, int | float) and not isinstance(item, bool):
        return float(item)
    raise ParamError(f"{item!r} is neither a parameter name nor a number")
