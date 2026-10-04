"""A legs strategy as plain data: what the API accepts, what a run is saved as,
and what an engine in another language reads.

One shape for all three, so a result can always be traced to the exact spec
that produced it, and a spec that round-trips here runs identically anywhere.
The shape is the API's: `side` is "buy"/"sell", times are "HH:MM[:SS]", sets
are lists.
"""

from __future__ import annotations

from datetime import time
from typing import Any

from optbt.data.models import Kind
from optbt.engine import Level, Side
from optbt.signals import Condition, EntrySignal, ExitSignal, Operand
from optbt.strategies.legs import (
    Adjustment,
    DayFilter,
    EntryTrigger,
    ExpiryChoice,
    LegsConfig,
    LegSpec,
    ReEntry,
    StrikeRule,
)

#: Bumped when the shape changes in a way an older reader would misread.
#: 2: the expiry is a choice (series, nth, min_left, days), set on the strategy
#: and optionally on a leg. Version 1's per-leg "week"/"next_week"/... still read.
#: 3: `trigger` (when the entry fires) and `reentry` (trying it again after the
#: position goes flat) on the strategy; `width_mult` and `sp_pct` on a leg's
#: strike. Missing on an older spec, both read as off.
#: 4: `entry_signal` and `exit_signal`, indicator conditions. Missing, none.
#: 5: "daily" as an expiry series - every listed expiry, where "weekly" is the
#: week's own. Identical on the NSE; they differ on a venue with dailies.
VERSION = 5


def _level_out(level: Level | None) -> dict[str, Any] | None:
    return None if level is None else {"kind": level.kind, "value": level.value}


def _level_in(raw: dict[str, Any] | None) -> Level | None:
    return None if raw is None else Level(raw["kind"], float(raw["value"]))


def _expiry_out(choice: ExpiryChoice | None) -> dict[str, Any] | None:
    if choice is None:
        return None
    return {
        "series": choice.series,
        "nth": choice.nth,
        "min_left": choice.min_left,
        "days": choice.days,
    }


def _expiry_in(raw: Any, days: int = 45) -> ExpiryChoice | None:
    if raw is None:
        return None
    if isinstance(raw, str):  # version 1
        return ExpiryChoice.from_legacy(raw, days)
    return ExpiryChoice(**raw)


def _time_in(raw: str | time) -> time:
    return raw if isinstance(raw, time) else time.fromisoformat(raw)


def _operand_out(op: Operand) -> dict[str, Any]:
    return {
        "kind": op.kind,
        "length": op.length,
        "mult": op.mult,
        "level": op.level,
        "value": op.value,
    }


def _conditions_out(conditions: tuple[Condition, ...]) -> list[dict[str, Any]]:
    return [
        {
            "left": _operand_out(c.left),
            "op": c.op,
            "right": _operand_out(c.right),
            "timeframe": c.timeframe,
        }
        for c in conditions
    ]


def _conditions_in(raw: list[dict[str, Any]] | None) -> tuple[Condition, ...]:
    return tuple(
        Condition(
            left=Operand(**c["left"]),
            op=c["op"],
            right=Operand(**c["right"]),
            timeframe=int(c.get("timeframe", 5)),
        )
        for c in raw or ()
    )


def to_dict(config: LegsConfig) -> dict[str, Any]:
    """The config as JSON-ready data."""
    return {
        "version": VERSION,
        "legs": [
            {
                "side": "buy" if leg.side is Side.BUY else "sell",
                "kind": str(leg.kind),
                "lots": leg.lots,
                "expiry": _expiry_out(leg.expiry),
                "strike": {
                    "mode": leg.strike.mode,
                    "offset": leg.strike.offset,
                    "premium": leg.strike.premium,
                    "pct": leg.strike.pct,
                    "delta": leg.strike.delta,
                    "width_mult": leg.strike.width_mult,
                    "sp_pct": leg.strike.sp_pct,
                },
                "stop": _level_out(leg.stop),
                "target": _level_out(leg.target),
            }
            for leg in config.legs
        ],
        "expiry": _expiry_out(config.expiry),
        "entry": config.entry.isoformat(),
        "exit": config.exit.isoformat(),
        "weekdays": sorted(config.weekdays),
        "hold": config.hold,
        "mtm_stop": config.mtm_stop,
        "mtm_target": config.mtm_target,
        "target_credit": config.target_credit,
        "stop_credit": config.stop_credit,
        "exit_dte": config.exit_dte,
        "trail_to_cost": config.trail_to_cost,
        "days": {
            "expiry_day": config.days.expiry_day,
            "dte_min": config.days.dte_min,
            "dte_max": config.days.dte_max,
            "vix_min": config.days.vix_min,
            "vix_max": config.days.vix_max,
            "vix_pct_min": config.days.vix_pct_min,
            "vix_pct_max": config.days.vix_pct_max,
            "vix_lookback": config.days.vix_lookback,
            "gap_min": config.days.gap_min,
            "gap_max": config.days.gap_max,
            "open_zones": sorted(config.days.open_zones),
        },
        "adjust": {
            "enabled": config.adjust.enabled,
            "near_points": config.adjust.near_points,
            "fall_from": config.adjust.fall_from,
            "fall_points": config.adjust.fall_points,
            "rise_from": config.adjust.rise_from,
            "rise_points": config.adjust.rise_points,
            "move_wing": config.adjust.move_wing,
            "max_per_trade": config.adjust.max_per_trade,
        },
        "equal_wings": config.equal_wings,
        "trigger": {
            "mode": config.trigger.mode,
            "move_pct": config.trigger.move_pct,
            "range_until": config.trigger.range_until.isoformat()
            if config.trigger.range_until
            else None,
        },
        "reentry": {
            "enabled": config.reentry.enabled,
            "trigger": config.reentry.trigger,
            "max_times": config.reentry.max_times,
        },
        "entry_signal": {
            "mode": config.entry_signal.mode,
            "join": config.entry_signal.join,
            "conditions": _conditions_out(config.entry_signal.conditions),
        },
        "exit_signal": {
            "join": config.exit_signal.join,
            "conditions": _conditions_out(config.exit_signal.conditions),
        },
    }


def from_dict(raw: dict[str, Any]) -> LegsConfig:
    """A config from data in `to_dict`'s shape. Missing optional keys take the defaults.

    Unknown keys are ignored here: validation of a request is the API's job, and
    a saved run carries more than its config.
    """
    version = raw.get("version", VERSION)
    if version > VERSION:
        raise ValueError(f"spec version {version} is newer than this reader ({VERSION})")
    days = raw.get("days") or {}
    adjust = raw.get("adjust") or {}
    trigger = raw.get("trigger") or {}
    reentry = raw.get("reentry") or {}
    entry_signal = raw.get("entry_signal") or {}
    exit_signal = raw.get("exit_signal") or {}
    defaults = LegsConfig(legs=())
    return LegsConfig(
        legs=tuple(
            LegSpec(
                side=Side.BUY if leg["side"] == "buy" else Side.SELL,
                kind=Kind(leg["kind"]),
                lots=int(leg.get("lots", 1)),
                expiry=_expiry_in(leg.get("expiry"), int(leg.get("expiry_days") or 45)),
                strike=StrikeRule(**(leg.get("strike") or {})),
                stop=_level_in(leg.get("stop")),
                target=_level_in(leg.get("target")),
            )
            for leg in raw["legs"]
        ),
        expiry=_expiry_in(raw.get("expiry")) or ExpiryChoice(),
        entry=_time_in(raw.get("entry", defaults.entry)),
        exit=_time_in(raw.get("exit", defaults.exit)),
        weekdays=frozenset(raw.get("weekdays", defaults.weekdays)),
        hold=raw.get("hold", defaults.hold),
        mtm_stop=raw.get("mtm_stop"),
        mtm_target=raw.get("mtm_target"),
        target_credit=raw.get("target_credit"),
        stop_credit=raw.get("stop_credit"),
        exit_dte=raw.get("exit_dte"),
        trail_to_cost=bool(raw.get("trail_to_cost", False)),
        days=DayFilter(**{**days, "open_zones": frozenset(days.get("open_zones", ()))}),
        adjust=Adjustment(**adjust),
        equal_wings=bool(raw.get("equal_wings", False)),
        trigger=EntryTrigger(
            **{
                **trigger,
                "range_until": (
                    _time_in(trigger["range_until"]) if trigger.get("range_until") else None
                ),
            }
        ),
        reentry=ReEntry(**reentry),
        entry_signal=EntrySignal(
            mode=entry_signal.get("mode", "take_if"),
            join=entry_signal.get("join", "all"),
            conditions=_conditions_in(entry_signal.get("conditions")),
        ),
        exit_signal=ExitSignal(
            join=exit_signal.get("join", "any"),
            conditions=_conditions_in(exit_signal.get("conditions")),
        ),
    )
