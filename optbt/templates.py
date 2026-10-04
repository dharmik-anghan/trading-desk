"""Built-in strategies to start from.

Held here rather than in the page so a template is the same thing as a saved
strategy - a spec in `optbt.spec`'s shape - and loads through the same path. Each
is partial: what it does not say takes the strategy's defaults, which the API
fills in by validating it.

Strikes are named by distance from the money in listed strikes, by delta or by
percent, never in index points, so every template means the same on any
underlying: four strikes out is 200 points on NIFTY and 1,000 on BTC.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Template:
    id: str
    name: str
    #: One line on what it does, for a tooltip.
    say: str
    spec: dict[str, Any]


def _leg(side: str, kind: str, offset: int = 0, *, stop_pct: float | None = None) -> dict[str, Any]:
    leg: dict[str, Any] = {"side": side, "kind": kind, "strike": {"mode": "atm", "offset": offset}}
    if stop_pct is not None:
        leg["stop"] = {"kind": "pct", "value": stop_pct}
    return leg


def _delta_leg(side: str, kind: str, delta: float) -> dict[str, Any]:
    return {"side": side, "kind": kind, "strike": {"mode": "delta", "delta": delta}}


TEMPLATES: tuple[Template, ...] = (
    Template(
        "short_straddle",
        "Short straddle",
        "sell ATM CE + PE, 25% stop each",
        {"legs": [_leg("sell", "CE", 0, stop_pct=0.25), _leg("sell", "PE", 0, stop_pct=0.25)]},
    ),
    Template(
        "short_strangle",
        "Short strangle",
        "sell OTM 2 CE + PE, 25% stop each",
        {"legs": [_leg("sell", "CE", 2, stop_pct=0.25), _leg("sell", "PE", 2, stop_pct=0.25)]},
    ),
    Template(
        "iron_condor",
        "Iron condor",
        "sell OTM 4, buy OTM 8, both sides",
        {
            "legs": [
                _leg("sell", "CE", 4),
                _leg("buy", "CE", 8),
                _leg("sell", "PE", 4),
                _leg("buy", "PE", 8),
            ]
        },
    ),
    Template(
        "condor_45dte",
        "45 DTE condor",
        "Monthly, entered at about 41 days to expiry (40-42): sell 0.30 delta, buy 0.17 "
        "delta, wings made equal. Positional; out at 50% of the credit, a loss equal to "
        "it, or 15 days to expiry. Moves the untested spread in at a wing.",
        {
            "legs": [
                _delta_leg("sell", "CE", 0.30),
                _delta_leg("buy", "CE", 0.17),
                _delta_leg("sell", "PE", 0.30),
                _delta_leg("buy", "PE", 0.17),
            ],
            "hold": "expiry",
            "expiry": {"series": "days", "nth": 1, "min_left": 0, "days": 45},
            "days": {"dte_min": 40, "dte_max": 42},
            "target_credit": 0.5,
            "stop_credit": 1.0,
            "adjust": {"enabled": True},
            "equal_wings": True,
            "exit_dte": 15,
        },
    ),
    Template(
        "iron_fly",
        "Iron fly",
        "sell ATM, buy OTM 4, both sides",
        {
            "legs": [
                _leg("sell", "CE", 0),
                _leg("buy", "CE", 4),
                _leg("sell", "PE", 0),
                _leg("buy", "PE", 4),
            ]
        },
    ),
    Template(
        "bull_put_spread",
        "Bull put spread",
        "sell OTM 1 PE, buy OTM 5 PE",
        {"legs": [_leg("sell", "PE", 1), _leg("buy", "PE", 5)]},
    ),
    Template(
        "bear_call_spread",
        "Bear call spread",
        "sell OTM 1 CE, buy OTM 5 CE",
        {"legs": [_leg("sell", "CE", 1), _leg("buy", "CE", 5)]},
    ),
)
