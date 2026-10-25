"""A strategy as data, so one can be built by choosing rather than by coding.

"EMA 5 crosses below the close, and RSI 14 is under 30, stop at the previous
candle's low" is a sentence made of parts, and the parts are the same few kinds
every time: something to measure, something to compare it to, and a way to
compare them. Written out as a tree of those parts, a strategy becomes something a
screen can build and something that can be stored, shared and compared - rather
than a Python class somebody has to write and deploy.

    operand    what to measure: an indicator, a price, a pivot level, a number,
               or arithmetic on any of those
    condition  a comparison, or several joined by and/or/not
    spec       entries for each side, an exit, a stop and a target

Three properties this shape buys, beyond not having to write code:

Every operand names its timeframe, so "hourly EMA 50 above hourly EMA 200 while
the five-minute RSI is under 30" is one expression rather than a special case, and
the engine works out which timeframes to align by reading the tree.

Nothing here can look ahead. Operands are read through `Frame`, which only ever
offers closed bars, so a condition cannot reach a price that had not happened -
whatever combination the person who built it chose.

A missing value is never a comparison. During warm-up an indicator is `None`, and
a comparison with one is false rather than an exception or a zero. A rule does not
trade while it is still blind.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from functools import cache
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from backtest.models import Trigger
from backtest.params import placeholders
from backtest.sessions import PRESETS, WEEKDAYS, Session, preset
from backtest.view import Frame, View
from marketdata.models import Interval

#: Indicators an operand may name.
#:
#: `pivot_gap` and `pivot_gap_rank` take a period that means something different
#: from the others: it is how many previous periods the rank is measured against,
#: not a smoothing length, and `pivot_gap` itself ignores it entirely.
INDICATORS = (
    "ema",
    "sma",
    "rsi",
    "atr",
    "pivot_gap",
    "pivot_gap_rank",
    # MACD: `length` is the fast period, `slow` the slow one (26), signal fixed at 9.
    "macd",
    "macd_signal",
    "macd_hist",
    # Bollinger bands round a `length` average, `mult` deviations wide (2).
    "bb_upper",
    "bb_lower",
    "bb_width",
    "adx",
    "plus_di",
    "minus_di",
    "vwap",
    "volume_sma",
    # The highest high and lowest low of the last `length` bars, this one included.
    "highest",
    "lowest",
)

#: Which of them do not average anything, so a period of 1 is not a mistake - or
#: take no period at all.
UNSMOOTHED = frozenset({"pivot_gap", "vwap"})

#: The ones a chart can draw. The newer indicators have a second number or their
#: own scale, and `backtest/lines.py` does not know them yet; listing them there
#: would draw a line of nothing.
DRAWABLE = frozenset({"ema", "sma", "rsi", "atr", "pivot_gap", "pivot_gap_rank"})

#: A bar's four prices.
FIELDS = ("open", "high", "low", "close")

#: What an operand may measure on a bar. A trigger rests an order at one of the
#: four prices; volume is a quantity, not a level, so it is only offered here.
MEASURES = (*FIELDS, "volume")

#: Pivot levels, by the names they are drawn with.
LEVELS = ("S3", "S2", "S1", "P", "R1", "R2", "R3")


class SpecError(ValueError):
    """A strategy that cannot be read, with a message meant for whoever built it."""


# ---------------------------------------------------------------------------
# Operands
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Operand:
    """Something with a number in it, at this instant.

    `ago` counts closed bars backwards on the operand's own timeframe, so
    `{"field": "low", "ago": 1}` is the previous candle's low - the commonest
    stop there is - and on an hourly operand it is the previous hour.
    """

    kind: str
    #: For an indicator: its name. For a price: the field. For a pivot: the level.
    name: str = ""
    length: int = 0
    value: float = 0.0
    ago: int = 0
    interval: Interval | None = None
    #: For arithmetic.
    left: Operand | None = None
    right: Operand | None = None
    op: str = ""
    #: The second number an indicator may take: MACD's slow period, a Bollinger
    #: band's width in deviations. Zero for the rest.
    extra: float = 0.0
    #: For "since": the condition whose last occurrence is being counted back to.
    when: Condition | None = None

    def timeframes(self) -> set[Interval]:
        found = {self.interval} if self.interval else set()
        for side in (self.left, self.right):
            if side is not None:
                found |= side.timeframes()
        if self.when is not None:
            found |= self.when.timeframes()
        return found

    def indicators(self) -> set[tuple[str, int, Interval | None]]:
        """Every indicator this operand reads, for drawing them on a chart.

        A strategy that trades an EMA crossing should show the two EMAs, and the
        only way to be sure the lines are the ones it read is to take them from
        the strategy itself rather than from a list somebody keeps in step by
        hand.
        """
        found: set[tuple[str, int, Interval | None]] = set()
        if self.kind == "indicator" and self.name in DRAWABLE:
            found.add((self.name, self.length, self.interval))
        for side in (self.left, self.right):
            if side is not None:
                found |= side.indicators()
        if self.when is not None:
            found |= self.when.indicators()
        return found

    def read(self, view: View) -> float | None:
        if self.kind == "value":
            return self.value
        if self.kind == "math":
            return self._arithmetic(view)
        if self.kind == "since":
            return self._since(view)

        frame = view.frame(self.interval) if self.interval else view.base
        if self.kind == "price":
            return frame.price(self.name, self.ago)
        if self.kind == "indicator":
            return self._indicator(frame)
        if self.kind == "pivot":
            levels = frame.pivots(self.ago)
            return levels.levels[self.name] if levels else None
        raise SpecError(f"{self.kind!r} is not something that can be measured")

    def _indicator(self, frame: Frame) -> float | None:
        if self.name == "ema":
            return frame.ema(self.length, self.ago)
        if self.name == "sma":
            return frame.sma(self.length, self.ago)
        if self.name == "rsi":
            return frame.rsi(self.length, self.ago)
        if self.name == "atr":
            return frame.atr(self.length, self.ago)
        if self.name == "pivot_gap":
            return frame.pivot_gap(self.ago)
        if self.name == "pivot_gap_rank":
            return frame.pivot_gap_rank(self.length, self.ago)
        if self.name in INDICATORS:
            return frame.line(self.name, self.length, self.extra, self.ago)
        raise SpecError(f"there is no indicator called {self.name!r}")

    def _since(self, view: View) -> float | None:
        """Closed bars since the condition last held; 0 if it holds now.

        The condition is asked of each earlier bar by reading everything in it
        that many bars further back, which is why it has to be on the traded
        timeframe: a higher timeframe does not move one bar at a time. None when
        it has not held within `length` bars - "a long time ago" is not a number.
        """
        assert self.when is not None
        for back in range(self.length + 1):
            if _shifted_by(self.when, self.ago + back).holds(view):
                return float(back)
        return None

    def _arithmetic(self, view: View) -> float | None:
        if self.left is None or self.right is None:
            raise SpecError("arithmetic needs both sides")
        a = self.left.read(view)
        b = self.right.read(view)
        # One unknown side makes the whole expression unknown, rather than zero.
        # "close - 2 x ATR" during ATR's warm-up is not the close.
        if a is None or b is None:
            return None
        if self.op == "+":
            return a + b
        if self.op == "-":
            return a - b
        if self.op == "*":
            return a * b
        if self.op == "/":
            return a / b if b else None
        raise SpecError(f"{self.op!r} is not an arithmetic operator")

    def describe(self) -> str:
        where = f" {self.interval}" if self.interval else ""
        back = f"[{self.ago} back]" if self.ago else ""
        if self.kind == "value":
            return f"{self.value:g}"
        if self.kind == "math":
            assert self.left and self.right
            return f"({self.left.describe()} {self.op} {self.right.describe()})"
        if self.kind == "since":
            assert self.when
            return f"bars since {self.when.describe()} (within {self.length}){back}"
        if self.kind == "indicator":
            if self.name == "pivot_gap":
                return f"pivot gap %{where}{back}"
            if self.name == "pivot_gap_rank":
                return f"pivot gap percentile over {self.length}{where}{back}"
            if self.name == "vwap":
                return f"VWAP{where}{back}"
            label = self.name.replace("_", " ").upper()
            if self.extra:
                return f"{label} {self.length}/{self.extra:g}{where}{back}"
            return f"{label} {self.length}{where}{back}"
        if self.kind == "price":
            return f"{self.name}{where}{back}"
        return f"pivot {self.name}{where}{back}"


# ---------------------------------------------------------------------------
# Conditions
# ---------------------------------------------------------------------------

COMPARISONS = ("above", "below", "crosses_above", "crosses_below", "equals")


@dataclass(frozen=True)
class Condition:
    """A comparison, or several joined together."""

    kind: str
    left: Operand | None = None
    right: Operand | None = None
    op: str = ""
    of: tuple[Condition, ...] = ()

    def timeframes(self) -> set[Interval]:
        found: set[Interval] = set()
        for side in (self.left, self.right):
            if side is not None:
                found |= side.timeframes()
        for inner in self.of:
            found |= inner.timeframes()
        return found

    def indicators(self) -> set[tuple[str, int, Interval | None]]:
        found: set[tuple[str, int, Interval | None]] = set()
        for side in (self.left, self.right):
            if side is not None:
                found |= side.indicators()
        for inner in self.of:
            found |= inner.indicators()
        return found

    def holds(self, view: View) -> bool:
        if self.kind == "all":
            return all(c.holds(view) for c in self.of)
        if self.kind == "any":
            return any(c.holds(view) for c in self.of)
        if self.kind == "not":
            return not all(c.holds(view) for c in self.of)
        if self.kind != "compare":
            raise SpecError(f"{self.kind!r} is not a kind of condition")

        if self.left is None or self.right is None:
            raise SpecError("a comparison needs both sides")
        now_left = self.left.read(view)
        now_right = self.right.read(view)
        if now_left is None or now_right is None:
            # Still warming up. Not tradeable rather than false-by-accident.
            return False

        if self.op == "above":
            return now_left > now_right
        if self.op == "below":
            return now_left < now_right
        if self.op == "equals":
            return now_left == now_right
        if self.op in ("crosses_above", "crosses_below"):
            return self._crossed(view, now_left, now_right)
        raise SpecError(f"{self.op!r} is not a comparison")

    def _crossed(self, view: View, now_left: float, now_right: float) -> bool:
        """A crossing is a statement about two bars, not one.

        Both sides are re-read one bar back. Without that, "EMA crosses below
        price" fires on every bar it is merely below, which is a completely
        different strategy - usually a much worse one, because it enters late and
        keeps entering.
        """
        assert self.left is not None and self.right is not None
        before_left = _shifted(self.left).read(view)
        before_right = _shifted(self.right).read(view)
        if before_left is None or before_right is None:
            return False
        if self.op == "crosses_above":
            return before_left <= before_right and now_left > now_right
        return before_left >= before_right and now_left < now_right

    def describe(self) -> str:
        if self.kind in ("all", "any"):
            joiner = " and " if self.kind == "all" else " or "
            return "(" + joiner.join(c.describe() for c in self.of) + ")"
        if self.kind == "not":
            return "not " + "".join(c.describe() for c in self.of)
        assert self.left and self.right
        return f"{self.left.describe()} {self.op.replace('_', ' ')} {self.right.describe()}"


def _shifted(operand: Operand) -> Operand:
    """The same operand, one bar earlier."""
    return _shifted_operand(operand, 1)


@cache
def _shifted_operand(operand: Operand, bars: int) -> Operand:
    """The same operand, `bars` bars earlier.

    Recursive through arithmetic, so "close - 2 x ATR" shifts both the close and
    the ATR rather than only the outermost thing.

    Cached: operands are frozen, and a crossing asks for the same shifted pair on
    every bar of a run - a million rebuilt dataclasses was a quarter of a
    three-year run's time.
    """
    if operand.kind == "value":
        return operand
    if operand.kind == "math":
        assert operand.left and operand.right
        return Operand(
            kind="math",
            op=operand.op,
            left=_shifted_operand(operand.left, bars),
            right=_shifted_operand(operand.right, bars),
        )
    return Operand(
        kind=operand.kind,
        name=operand.name,
        length=operand.length,
        value=operand.value,
        ago=operand.ago + bars,
        interval=operand.interval,
        extra=operand.extra,
        when=operand.when,
    )


@cache
def _shifted_by(condition: Condition, bars: int) -> Condition:
    """The same condition, asked as of `bars` bars earlier."""
    if bars == 0:
        return condition
    if condition.kind != "compare":
        return Condition(
            kind=condition.kind, of=tuple(_shifted_by(c, bars) for c in condition.of)
        )
    assert condition.left and condition.right
    return Condition(
        kind="compare",
        op=condition.op,
        left=_shifted_operand(condition.left, bars),
        right=_shifted_operand(condition.right, bars),
    )


# ---------------------------------------------------------------------------
# Stops and targets
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Level:
    """Where a stop or a target goes, worked out at the moment of entry.

    A level is not a condition: it is a price, computed once when the position
    opens and then left alone. Recomputing it every bar would be a trailing stop,
    which is a different thing and should be asked for by name.
    """

    kind: str
    #: For "percent": how far, as a percentage. For "atr": the multiple. For
    #: "swing": the widest stop allowed, as a percentage of the entry (0 = any).
    value: float = 0.0
    length: int = 14
    #: For "candle": which price, and how many bars back. "candle_mid" takes the
    #: bars back too, and uses `value` as the ATR multiple past which it halves.
    name: str = "low"
    ago: int = 1
    interval: Interval | None = None

    def timeframes(self) -> set[Interval]:
        return {self.interval} if self.interval else set()

    def _candle_or_mid(self, frame: Frame, entry: float, long: bool) -> float | None:
        """The far end of the candle `ago` back - its low for a long, its high for
        a short - or its midpoint when that is further from the entry than
        `value` ATRs. A wide setup candle would otherwise set a stop too big to
        be worth the trade; halving it keeps the trade at half the risk."""
        high, low = frame.price("high", self.ago), frame.price("low", self.ago)
        if high is None or low is None:
            return None
        far = low if long else high
        if self.value:
            width = frame.atr(self.length)
            if width is not None and abs(far - entry) > self.value * width:
                return (high + low) / 2
        return far

    def price(
        self, view: View, entry: float, long: bool, *, stop: float | None = None
    ) -> float | None:
        """The level, in money.

        `long` decides the direction: a stop is below the entry for a long and
        above it for a short, and getting that backwards produces a position that
        closes itself on the next tick.
        """
        away = -1.0 if long else 1.0
        frame = view.frame(self.interval) if self.interval else view.base

        if self.kind == "percent":
            return entry * (1 + away * self.value / 100.0)
        if self.kind == "candle":
            # "far" is the candle's extreme against the trade: its low for a long,
            # its high for a short - one stop that reads right for both sides.
            name = ("low" if long else "high") if self.name == "far" else self.name
            return frame.price(name, self.ago)
        if self.kind == "candle_mid":
            return self._candle_or_mid(frame, entry, long)
        if self.kind == "atr":
            width = frame.atr(self.length)
            return entry + away * width * self.value if width is not None else None
        if self.kind == "pivot":
            levels = frame.pivots()
            return levels.levels[self.name] if levels else None
        if self.kind == "swing":
            found = frame.swing(long, self.length)
            # `value` caps how far away the stop may be, as a percentage of the
            # entry. A swing further off than that is a trade not worth the risk,
            # so there is no level and the rule stands aside.
            if found is not None and self.value and abs(entry - found) / entry * 100 > self.value:
                return None
            return found
        if self.kind == "reward":
            # A target expressed as a multiple of the risk taken, which needs the
            # stop to exist first.
            if stop is None:
                return None
            return entry - away * abs(entry - stop) * self.value
        raise SpecError(f"{self.kind!r} is not a kind of level")


# ---------------------------------------------------------------------------
# The strategy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StrategySpec:
    """Everything a run needs, as data."""

    name: str = "Untitled"
    interval: Interval = Interval.M5
    long_entry: Condition | None = None
    short_entry: Condition | None = None
    long_exit: Condition | None = None
    short_exit: Condition | None = None
    stop: Level | None = None
    target: Level | None = None
    #: Hours during which entries are allowed. Empty means all of them, which is
    #: right for a perpetual and wrong for almost everything else.
    sessions: tuple[Session, ...] = ()
    #: What has to happen after the entry conditions before a trade is taken.
    #: None means take it at the next open.
    trigger: Trigger | None = None
    #: Whether an open position is closed when the session ends. What an intraday
    #: strategy does, and the difference between "I trade the London session" and
    #: "I open trades during London and hold them through Tokyo".
    close_outside_session: bool = False
    notes: str = ""

    @property
    def context(self) -> tuple[Interval, ...]:
        """Higher timeframes this strategy reads, worked out from its own parts.

        Derived rather than declared: a person building this on a screen picks an
        hourly EMA, and should not then have to remember to list the hourly
        somewhere else. Longest first, which is only for a tidy display.
        """
        found: set[Interval] = set()
        for part in (self.long_entry, self.short_entry, self.long_exit, self.short_exit):
            if part is not None:
                found |= part.timeframes()
        for level in (self.stop, self.target):
            if level is not None:
                found |= level.timeframes()
        found.discard(self.interval)
        return tuple(sorted(found, key=lambda i: i.seconds, reverse=True))

    def indicators(self) -> list[tuple[str, int, Interval | None]]:
        """Every indicator the strategy reads, longest period first.

        Ordered so a chart draws the slow line under the fast one, which is the
        way round that keeps a crossing legible.
        """
        found: set[tuple[str, int, Interval | None]] = set()
        for part in (self.long_entry, self.short_entry, self.long_exit, self.short_exit):
            if part is not None:
                found |= part.indicators()
        for lev in (self.stop, self.target):
            if lev is not None and lev.kind == "atr":
                found.add(("atr", lev.length, lev.interval))
        return sorted(found, key=lambda i: (-i[1], i[0]))

    def check(self) -> list[str]:
        """Everything wrong with this, in words, before a run is attempted.

        All of it at once rather than the first problem: somebody building a
        strategy on a screen should see every mistake in one pass.
        """
        problems: list[str] = []
        if self.long_entry is None and self.short_entry is None:
            problems.append("Nothing to enter on: set a long or a short entry")
        for interval in self.context:
            if interval.seconds < self.interval.seconds:
                problems.append(
                    f"{interval} is shorter than the {self.interval} being traded; "
                    "a condition can only look at a longer timeframe than the one it trades"
                )
        if (
            self.long_entry is None
            and self.short_entry is None
            or (self.long_exit is None and self.short_exit is None)
        ) and self.stop is None and self.target is None:
            problems.append(
                "Nothing to exit on: without an exit condition, a stop or a target, "
                "a position is only closed when the data runs out"
            )
        return problems


# ---------------------------------------------------------------------------
# Reading one from a screen
# ---------------------------------------------------------------------------


def operand(raw: Any) -> Operand:
    """One operand, from the shape a UI posts.

    A bare number is allowed as shorthand, because `{"kind": "value", "value": 30}`
    is a silly way to write 30.
    """
    if isinstance(raw, int | float) and not isinstance(raw, bool):
        return Operand(kind="value", value=float(raw))
    if not isinstance(raw, dict):
        raise SpecError(f"{raw!r} is not an operand")

    kind = str(raw.get("kind", "")).lower()
    interval = _interval(raw.get("tf") or raw.get("interval"))
    ago = _whole(raw.get("ago", 0), "ago")

    if kind == "value":
        return Operand(kind="value", value=float(raw.get("value", 0.0)))
    if kind == "math":
        op = str(raw.get("op", ""))
        if op not in ("+", "-", "*", "/"):
            raise SpecError(f"{op!r} is not one of + - * /")
        return Operand(
            kind="math", op=op, left=operand(raw.get("left")), right=operand(raw.get("right"))
        )
    if kind == "price":
        name = str(raw.get("field", "close")).lower()
        if name not in MEASURES:
            raise SpecError(f"a candle has no {name!r}; pick one of {', '.join(MEASURES)}")
        return Operand(kind="price", name=name, ago=ago, interval=interval)
    if kind == "since":
        when = condition(raw.get("when"))
        if when.timeframes():
            raise SpecError(
                "bars since a condition can only count on the timeframe being traded; "
                "leave the timeframe off the condition's parts"
            )
        if _mentions_since(when):
            raise SpecError("bars since cannot be built from another bars since")
        reach = _whole(raw.get("max", 50), "max")
        if not 1 <= reach <= 1000:
            raise SpecError("bars since looks back between 1 and 1000 bars")
        return Operand(kind="since", length=reach, ago=ago, when=when)
    if kind == "indicator":
        name = str(raw.get("name", "")).lower()
        if name not in INDICATORS:
            raise SpecError(
                f"there is no indicator called {name!r}; "
                f"pick one of {', '.join(INDICATORS)}"
            )
        length = _whole(raw.get("length", 14), "length")
        if length < 1 and name not in UNSMOOTHED:
            raise SpecError(f"{name.upper()} needs a period of at least 1")
        extra = _extra(name, raw, length)
        return Operand(
            kind="indicator", name=name, length=length, ago=ago, interval=interval, extra=extra
        )
    if kind == "pivot":
        name = str(raw.get("level", "P")).upper()
        if name not in LEVELS:
            raise SpecError(f"{name!r} is not a pivot level; pick one of {', '.join(LEVELS)}")
        return Operand(kind="pivot", name=name, ago=ago, interval=interval)
    raise SpecError(f"{kind!r} is not something that can be measured")


def _extra(name: str, raw: dict[str, Any], length: int) -> float:
    """The second number an indicator takes, defaulted to the usual one."""
    if name.startswith("macd"):
        slow = _whole(raw.get("slow", 26), "slow")
        if slow <= length:
            raise SpecError(
                f"MACD's slow period ({slow}) has to be longer than its fast ({length})"
            )
        return float(slow)
    if name.startswith("bb_"):
        mult = float(raw.get("mult", 2.0))
        if mult <= 0:
            raise SpecError("a Bollinger band has to be more than zero deviations wide")
        return mult
    return 0.0


def _mentions_since(cond: Condition) -> bool:
    def operand_has(o: Operand | None) -> bool:
        if o is None:
            return False
        return o.kind == "since" or operand_has(o.left) or operand_has(o.right)

    inner = any(_mentions_since(c) for c in cond.of)
    return operand_has(cond.left) or operand_has(cond.right) or inner


def condition(raw: Any) -> Condition:
    """One condition, from the shape a UI posts."""
    if not isinstance(raw, dict):
        raise SpecError(f"{raw!r} is not a condition")

    for joiner in ("all", "any"):
        if joiner in raw:
            parts = raw[joiner]
            if not isinstance(parts, list) or not parts:
                raise SpecError(f"{joiner!r} needs a list of conditions")
            return Condition(kind=joiner, of=tuple(condition(p) for p in parts))
    if "not" in raw:
        return Condition(kind="not", of=(condition(raw["not"]),))

    op = str(raw.get("op", "")).lower()
    if op not in COMPARISONS:
        raise SpecError(f"{op!r} is not a comparison; pick one of {', '.join(COMPARISONS)}")
    return Condition(
        kind="compare", left=operand(raw.get("left")), right=operand(raw.get("right")), op=op
    )


def level(raw: Any) -> Level | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SpecError(f"{raw!r} is not a stop or a target")
    kind = str(raw.get("kind", "")).lower()
    if kind not in ("percent", "candle", "candle_mid", "atr", "pivot", "swing", "reward"):
        raise SpecError(
            f"{kind!r} is not a kind of level; "
            "pick percent, candle, candle_mid, atr, pivot, swing or reward"
        )
    return Level(
        kind=kind,
        value=float(raw.get("value", 0.0)),
        length=_whole(raw.get("length", 5 if kind == "swing" else 14), "length"),
        name=str(raw.get("field") or raw.get("level") or "low"),
        ago=_whole(raw.get("ago", 1), "ago"),
        interval=_interval(raw.get("tf") or raw.get("interval")),
    )


def trigger(raw: Any) -> Trigger | None:
    """What has to happen after a setup, from the shape a UI posts.

    Absent means the entry is taken at the next open, which is right for a rule
    that has already decided. A trigger says the conditions were only a setup,
    and the trade waits for price to confirm it.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SpecError(f"{raw!r} is not an entry trigger")

    kind = str(raw.get("kind", "break")).lower()
    if kind in ("", "open", "next_open", "none"):
        return None
    if kind != "break":
        raise SpecError(
            f"{kind!r} is not a kind of trigger; use \"break\" to wait for price to "
            "break a level, or leave it out to enter at the next open"
        )

    field = str(raw.get("field", "high")).lower()
    if field not in FIELDS:
        raise SpecError(f"a candle has no {field!r}; pick one of {', '.join(FIELDS)}")
    within = _whole(raw.get("within", 3), "within")
    if within < 1:
        raise SpecError("a trigger has to rest for at least one bar")
    if within > 500:
        raise SpecError("a trigger resting for more than 500 bars is not a trigger")
    return Trigger(
        field=field,
        ago=_whole(raw.get("ago", 0), "ago"),
        within=within,
        buffer_bps=float(raw.get("buffer_bps", 0.0)),
    )


def sessions(raw: Any) -> tuple[Session, ...]:
    """The hours to trade in, from the shape a UI posts.

    Either a preset by name - "london", "newyork" - or a window of its own with a
    zone. A zone is required on a custom window rather than defaulted to UTC:
    somebody writing 08:00 means eight o'clock somewhere, and guessing which
    somewhere is how a session ends up an hour out for half the year.
    """
    if raw is None or raw == []:
        return ()
    if not isinstance(raw, list):
        raise SpecError("sessions are a list")

    out: list[Session] = []
    for entry in raw:
        if isinstance(entry, str):
            named = preset(entry)
            if named is None:
                raise SpecError(
                    f"{entry!r} is not a session; pick one of {', '.join(PRESETS)}, "
                    "or give a window of your own"
                )
            out.append(named)
            continue
        if not isinstance(entry, dict):
            raise SpecError(f"{entry!r} is not a session")
        if entry.get("name") and "start" not in entry:
            named = preset(str(entry["name"]))
            if named is not None:
                out.append(named)
                continue
        zone = str(entry.get("tz") or "")
        if not zone:
            raise SpecError("a session of your own needs a timezone, such as Europe/London")
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError):
            raise SpecError(f"{zone!r} is not a timezone this machine knows") from None
        out.append(
            Session(
                name=str(entry.get("name") or zone),
                start=_clock(entry.get("start"), "start"),
                end=_clock(entry.get("end"), "end"),
                tz=zone,
                days=frozenset(int(d) for d in entry.get("days", WEEKDAYS)),
            )
        )
    return tuple(out)


def _clock(raw: Any, what: str) -> time:
    """A time of day written "08:30"."""
    try:
        hour, _, minute = str(raw).partition(":")
        return time(int(hour), int(minute or 0))
    except (TypeError, ValueError):
        raise SpecError(f"{what} has to be a time like 08:30, not {raw!r}") from None


def parse(raw: dict[str, Any]) -> StrategySpec:
    """A whole strategy, from the shape a UI posts.

    Raises `SpecError` with a message a person can act on. This is the boundary
    between something typed on a screen and something that runs, so every message
    here says what to do rather than what went wrong internally.
    """
    if not isinstance(raw, dict):
        raise SpecError("a strategy is an object")
    unbound = placeholders(raw)
    if unbound:
        # A parametrised document is a family of strategies, not one. Which one is
        # chosen by `params.bind`; guessing a value here would run something
        # nobody asked for.
        raise SpecError(
            f"{', '.join(sorted(unbound))} still has to be given a value; "
            "bind the parameters before running"
        )

    spec = StrategySpec(
        name=str(raw.get("name") or "Untitled"),
        interval=_interval(raw.get("interval") or raw.get("tf")) or Interval.M5,
        long_entry=condition(raw["long_entry"]) if raw.get("long_entry") else None,
        short_entry=condition(raw["short_entry"]) if raw.get("short_entry") else None,
        long_exit=condition(raw["long_exit"]) if raw.get("long_exit") else None,
        short_exit=condition(raw["short_exit"]) if raw.get("short_exit") else None,
        stop=level(raw.get("stop")),
        target=level(raw.get("target")),
        trigger=trigger(raw.get("trigger")),
        sessions=sessions(raw.get("sessions")),
        close_outside_session=bool(raw.get("close_outside_session")),
        notes=str(raw.get("notes") or ""),
    )
    problems = spec.check()
    if problems:
        raise SpecError("; ".join(problems))
    return spec


def _interval(raw: Any) -> Interval | None:
    if raw is None or raw == "":
        return None
    try:
        return Interval(str(raw))
    except ValueError:
        raise SpecError(
            f"{raw!r} is not a bar size; pick one of {', '.join(Interval)}"
        ) from None


def _whole(raw: Any, what: str) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise SpecError(f"{what} has to be a whole number, not {raw!r}") from None
    if value < 0:
        raise SpecError(f"{what} cannot be negative")
    return value
