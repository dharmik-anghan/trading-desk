"""Indicator conditions on the index: "spot above EMA 20 on 5m", "RSI 14 on 15m
crosses below 60", "spot below yesterday's R1".

A condition compares two operands on one timeframe's candles. An entry rule
uses them to take the trade only if they hold, skip it if they hold, or wait
for them before entering; an exit rule closes the position when they hold.

Only finished candles count. A 5-minute candle from 09:15 is read from the
decision at 09:20 (the 09:19 bar's close) onwards, so nothing here can see a
price the strategy could not have seen. Candles are aligned to the 09:15 open,
as NSE charts draw them; a session's last one may be short (15:15-15:29 on an
hour chart) and is finished with the session.

The indicators update one candle at a time, with the same arithmetic as
`analytics/indicators.py`, so the value at a candle is the very float the
whole-series function gives over the same candles (`tests/optbt/test_signals.py`
checks it).
They are warmed on the sessions before the run's first day - enough candles
for an EMA's seed to have washed out - so the first days of a run read the same
EMA a chart would, give or take the far past it never saw.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Literal, Protocol

from analytics.indicators import _rsi_from
from optbt.market import SESSION_OPEN, Bar

if TYPE_CHECKING:
    from optbt.context import Context
    from optbt.market import View

OperandKind = Literal["price", "ema", "sma", "rsi", "supertrend", "level", "number"]
Op = Literal["above", "below", "crosses_above", "crosses_below"]

#: Daily levels, all from before today's open bar: classic pivots from
#: yesterday's high, low and close; yesterday's high, low and close themselves;
#: and today's open.
LEVELS = ("P", "R1", "R2", "R3", "S1", "S2", "S3", "PDH", "PDL", "PDC", "DO")

#: Timeframes a condition may be read on, in minutes.
TIMEFRAMES = (1, 3, 5, 10, 15, 30, 60)

#: Sessions of warm-up are capped here: a year of candles is enough for anything.
MAX_WARMUP_SESSIONS = 250

#: Minutes in a full session, for turning a candle count into sessions.
SESSION_MINUTES = 375


@dataclass(frozen=True)
class Operand:
    """One side of a condition. Only the fields its kind uses matter."""

    kind: OperandKind = "price"
    #: Period, for ema / sma / rsi / supertrend.
    length: int = 20
    #: ATR multiple, for supertrend.
    mult: float = 3.0
    #: One of LEVELS, for level.
    level: str = "P"
    #: The number itself, for number.
    value: float = 0.0

    @property
    def key(self) -> tuple[object, ...]:
        """What makes two operands the same series: kind and the fields it uses."""
        if self.kind in ("ema", "sma", "rsi"):
            return (self.kind, self.length)
        if self.kind == "supertrend":
            return (self.kind, self.length, self.mult)
        if self.kind == "level":
            return (self.kind, self.level)
        if self.kind == "number":
            return (self.kind, self.value)
        return (self.kind,)

    @property
    def label(self) -> str:
        if self.kind == "price":
            return "spot"
        if self.kind == "supertrend":
            return f"Supertrend {self.length},{self.mult:g}"
        if self.kind == "level":
            return self.level
        if self.kind == "number":
            return f"{self.value:g}"
        return f"{self.kind.upper()} {self.length}"

    def candles_needed(self) -> int:
        """Candles before the value is trustworthy: defined, and with an
        exponential seed washed out to well under a percent of its weight."""
        if self.kind == "sma":
            return self.length
        if self.kind in ("ema", "rsi", "supertrend"):
            return 5 * self.length + 1
        return 1


@dataclass(frozen=True)
class Condition:
    left: Operand
    op: Op
    right: Operand
    #: Minutes per candle; one of TIMEFRAMES.
    timeframe: int = 5

    @property
    def is_cross(self) -> bool:
        return self.op in ("crosses_above", "crosses_below")


@dataclass(frozen=True)
class EntrySignal:
    """Conditions on the entry. Empty is no conditions.

    "take_if": when the entry would fire, take it only if they hold, else skip
    the day. "skip_if": skip the day if they hold. "wait": from the entry time
    until the exit, enter on the first bar they hold - no five-minute grace,
    and a day they never hold is not traded.
    """

    mode: Literal["take_if", "skip_if", "wait"] = "take_if"
    join: Literal["all", "any"] = "all"
    conditions: tuple[Condition, ...] = ()


@dataclass(frozen=True)
class ExitSignal:
    """Close the whole position when these hold. Empty is no conditions."""

    join: Literal["all", "any"] = "any"
    conditions: tuple[Condition, ...] = ()


# ------------------------------------------------------------- indicators


@dataclass(frozen=True)
class _Candle:
    day: date
    high: float
    low: float
    close: float


class _Indicator(Protocol):
    def push(self, candle: _Candle) -> float | None: ...


class _Sma:
    def __init__(self, length: int) -> None:
        self.length = length
        self.window: deque[float] = deque()
        self.running = 0.0

    def push(self, candle: _Candle) -> float | None:
        x = candle.close
        self.window.append(x)
        if len(self.window) < self.length:
            return None
        if len(self.window) == self.length:
            # sum(), not a running total, for the seed: since Python 3.12 it
            # compensates for rounding, and the whole-series function uses it.
            self.running = sum(self.window)
        else:
            self.running += x - self.window.popleft()
        return self.running / self.length


class _Ema:
    def __init__(self, length: int) -> None:
        self.length = length
        self.k = 2.0 / (length + 1)
        self.seed: list[float] = []
        self.value: float | None = None

    def push(self, candle: _Candle) -> float | None:
        x = candle.close
        if self.value is None:
            self.seed.append(x)
            if len(self.seed) == self.length:
                self.value = sum(self.seed) / self.length
        else:
            self.value = (x - self.value) * self.k + self.value
        return self.value


class _Rsi:
    """Wilder's, as `analytics.indicators.rsi`."""

    def __init__(self, length: int) -> None:
        self.length = length
        self.previous: float | None = None
        self.changes = 0
        self.gains = 0.0
        self.losses = 0.0
        self.value: float | None = None

    def push(self, candle: _Candle) -> float | None:
        x = candle.close
        if self.previous is None:
            self.previous = x
            return None
        change = x - self.previous
        self.previous = x
        n = self.length
        if self.changes < n:
            self.gains += max(change, 0.0)
            self.losses += max(-change, 0.0)
            self.changes += 1
            if self.changes == n:
                self.gains /= n
                self.losses /= n
                self.value = _rsi_from(self.gains, self.losses)
            return self.value
        self.gains = (self.gains * (n - 1) + max(change, 0.0)) / n
        self.losses = (self.losses * (n - 1) + max(-change, 0.0)) / n
        self.value = _rsi_from(self.gains, self.losses)
        return self.value


class _Atr:
    """Wilder's, as `analytics.indicators.atr`."""

    def __init__(self, length: int) -> None:
        self.length = length
        self.previous_close: float | None = None
        self.seed: list[float] = []
        self.value: float | None = None

    def push(self, candle: _Candle) -> float | None:
        if self.previous_close is None:
            self.previous_close = candle.close
            return None
        pc = self.previous_close
        self.previous_close = candle.close
        tr = max(candle.high - candle.low, abs(candle.high - pc), abs(candle.low - pc))
        if self.value is None:
            self.seed.append(tr)
            if len(self.seed) == self.length:
                self.value = sum(self.seed) / self.length
        else:
            self.value = (self.value * (self.length - 1) + tr) / self.length
        return self.value


class _Supertrend:
    """As `analytics.indicators.supertrend`."""

    def __init__(self, length: int, mult: float) -> None:
        self.atr = _Atr(length)
        self.mult = mult
        self.upper = self.lower = self.line = 0.0
        self.started = False
        self.previous_close: float | None = None

    def push(self, candle: _Candle) -> float | None:
        a = self.atr.push(candle)
        previous_close, self.previous_close = self.previous_close, candle.close
        if a is None:
            return None
        mid = (candle.high + candle.low) / 2.0
        basic_upper, basic_lower = mid + self.mult * a, mid - self.mult * a
        if not self.started:
            self.upper, self.lower, self.line = basic_upper, basic_lower, basic_upper
            self.started = True
            return self.line
        assert previous_close is not None
        was_upper = self.line == self.upper
        if basic_upper < self.upper or previous_close > self.upper:
            self.upper = basic_upper
        if basic_lower > self.lower or previous_close < self.lower:
            self.lower = basic_lower
        if was_upper:
            self.line = self.lower if candle.close > self.upper else self.upper
        else:
            self.line = self.upper if candle.close < self.lower else self.lower
        return self.line


def _indicator(op: Operand) -> _Indicator | None:
    if op.kind == "ema":
        return _Ema(op.length)
    if op.kind == "sma":
        return _Sma(op.length)
    if op.kind == "rsi":
        return _Rsi(op.length)
    if op.kind == "supertrend":
        return _Supertrend(op.length, op.mult)
    return None


def _level(context: Context, day: date, name: str) -> float | None:
    d = context.day(day)
    if d is None:
        return None
    if name == "DO":
        return d.open
    if name == "PDH":
        return d.prev_high
    if name == "PDL":
        return d.prev_low
    if name == "PDC":
        return d.prev_close
    return d.pivots.level(name) if d.pivots else None


# ------------------------------------------------------------------ frames


class _Frame:
    """One timeframe: minute bars rolled into candles, and every operand read
    on it, as of the last finished candle and the one before."""

    def __init__(self, minutes: int, operands: Iterable[Operand]) -> None:
        self.minutes = minutes
        self.operands = {op.key: op for op in operands}
        self._indicators = {
            key: ind for key, op in self.operands.items() if (ind := _indicator(op)) is not None
        }
        self.cur: dict[tuple[object, ...], float | None] = {}
        self.prev: dict[tuple[object, ...], float | None] = {}
        #: The last minute bar of the most recently finished candle.
        self.completed_at: datetime | None = None
        self._bucket: tuple[date, int] | None = None
        self._high = self._low = self._close = 0.0
        self._last_ts: datetime | None = None

    def add(self, bar: Bar, last_of_day: bool, context: Callable[[], Context]) -> None:
        minute = (bar.ts.hour * 60 + bar.ts.minute) - (SESSION_OPEN.hour * 60 + SESSION_OPEN.minute)
        bucket = (bar.ts.date(), minute // self.minutes)
        if self._bucket is not None and bucket != self._bucket:
            self._complete(context)  # a bar missing at the candle's end
        if self._bucket is None:
            self._bucket = bucket
            self._high, self._low = bar.high, bar.low
        else:
            self._high, self._low = max(self._high, bar.high), min(self._low, bar.low)
        self._close = bar.close
        self._last_ts = bar.ts
        if (minute + 1) % self.minutes == 0 or last_of_day:
            self._complete(context)

    def flush(self, context: Callable[[], Context]) -> None:
        if self._bucket is not None:
            self._complete(context)

    def _complete(self, context: Callable[[], Context]) -> None:
        assert self._bucket is not None
        candle = _Candle(self._bucket[0], self._high, self._low, self._close)
        values: dict[tuple[object, ...], float | None] = {}
        for key, op in self.operands.items():
            if op.kind == "price":
                values[key] = candle.close
            elif op.kind == "number":
                values[key] = op.value
            elif op.kind == "level":
                values[key] = _level(context(), candle.day, op.level)
            else:
                values[key] = self._indicators[key].push(candle)
        self.prev, self.cur = self.cur, values
        self.completed_at = self._last_ts
        self._bucket = None


class Signals:
    """Every timeframe the conditions read, fed one closed minute bar at a time."""

    def __init__(self, conditions: Iterable[Condition]) -> None:
        by_tf: dict[int, list[Operand]] = {}
        for c in conditions:
            by_tf.setdefault(c.timeframe, []).extend((c.left, c.right))
        self._frames = {tf: _Frame(tf, ops) for tf, ops in by_tf.items()}
        self._warm = False

    def warmup_sessions(self) -> int:
        need = 0
        for tf, frame in self._frames.items():
            candles = max((op.candles_needed() for op in frame.operands.values()), default=1)
            need = max(need, math.ceil(candles * tf / SESSION_MINUTES) + 1)
        return min(need, MAX_WARMUP_SESSIONS)

    def on_day(self, view: View) -> None:
        context = lambda: view.context  # noqa: E731
        for frame in self._frames.values():
            frame.flush(context)
        if not self._warm:
            self._warm = True
            for bars in view.sessions_before(self.warmup_sessions()):
                self._feed(bars, context)

    def on_bar(self, view: View) -> None:
        bar = view.bar()
        last = view.bars_left == 0
        for frame in self._frames.values():
            frame.add(bar, last, lambda: view.context)

    def _feed(self, bars: Sequence[Bar], context: Callable[[], Context]) -> None:
        for i, bar in enumerate(bars):
            for frame in self._frames.values():
                frame.add(bar, i == len(bars) - 1, context)

    def check(self, c: Condition, since: datetime | None = None) -> bool | None:
        """Whether a condition holds on its timeframe's last finished candle.

        None when either side has no value yet. With `since`, a cross only
        counts on a candle finished at or after it - a position opened at 10:02
        is not closed by a cross the 09:55-09:59 candle made before it existed.
        """
        f = self._frames[c.timeframe]
        left, right = f.cur.get(c.left.key), f.cur.get(c.right.key)
        if left is None or right is None:
            return None
        if c.op == "above":
            return left > right
        if c.op == "below":
            return left < right
        if since is not None and (f.completed_at is None or f.completed_at < since):
            return False
        lp, rp = f.prev.get(c.left.key), f.prev.get(c.right.key)
        if lp is None or rp is None:
            return None
        if c.op == "crosses_above":
            return lp <= rp and left > right
        return lp >= rp and left < right

    def met(
        self,
        conditions: Sequence[Condition],
        join: Literal["all", "any"],
        since: datetime | None = None,
    ) -> bool | None:
        """All (or any) of the conditions together. None when that cannot be
        told yet: some unknown, and none of the known ones decide it."""
        results = [self.check(c, since) for c in conditions]
        decider, other = (False, True) if join == "all" else (True, False)
        if decider in results:
            return decider
        if None in results:
            return None
        return other

    def readout(self, c: Condition) -> str:
        """The condition with the numbers it was judged on, for a trade's log."""
        f = self._frames[c.timeframe]

        def side(op: Operand) -> str:
            v = f.cur.get(op.key)
            if op.kind == "number" or v is None:
                return op.label if v is not None else f"{op.label} (n/a)"
            return f"{op.label} {v:,.2f}"

        words = c.op.replace("_", " ")
        return f"{side(c.left)} {words} {side(c.right)} on {c.timeframe}m"
