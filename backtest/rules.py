"""Turning a built strategy into something the engine can run.

This is the whole of the bridge: the engine asks a rule what it wants at each
bar, and a spec answers by evaluating its conditions. There is no library of
hand-written strategies, because a strategy is a choice of conditions and those
are made on a screen.

The one piece of judgement here is what happens when both sides say enter at the
same bar. That is a contradiction in the strategy rather than a situation to
resolve cleverly, so it is refused: no position, and the reason says so. Picking
a side would hide the mistake behind a result.
"""

from __future__ import annotations

from backtest.market import Side
from backtest.models import Intent, Position
from backtest.sessions import in_any
from backtest.spec import StrategySpec
from backtest.view import View
from marketdata.models import Interval


class SpecRule:
    """A strategy built from conditions, as the engine's `Rule`."""

    def __init__(self, spec: StrategySpec) -> None:
        self.spec = spec
        self.name = spec.name
        self.context: tuple[Interval, ...] = spec.context

    def entry(self, view: View) -> Intent:
        spec = self.spec
        # Judged on the bar's close, the instant the decision is made, and before
        # anything else: a condition that fires outside the hours being traded is
        # not a signal, so there is nothing to evaluate.
        if view.at is not None and not in_any(spec.sessions, view.at):
            return Intent.nothing()
        wants_long = spec.long_entry is not None and spec.long_entry.holds(view)
        wants_short = spec.short_entry is not None and spec.short_entry.holds(view)
        if wants_long and wants_short:
            # Both at once is a strategy that contradicts itself. Standing aside
            # keeps that visible instead of resolving it into a trade.
            return Intent.nothing()
        if not wants_long and not wants_short:
            return Intent.nothing()

        side = Side.LONG if wants_long else Side.SHORT
        entry = _reference(spec, view, side)
        if entry is None:
            return Intent.nothing()

        long = side is Side.LONG
        stop = spec.stop.price(view, entry, long) if spec.stop else None
        # A stop was asked for but cannot be worked out - an ATR still warming up,
        # a swing too wide. Trading without it would be a different strategy with
        # unlimited risk, so the setup is skipped.
        if spec.stop is not None and stop is None:
            return Intent.nothing()
        # A stop on the wrong side of the entry would close the position the
        # instant it opened - which is what "stop at the previous candle's low"
        # means on a short. Dropped rather than honoured, and the trade taken
        # without it, because a stop that cannot be placed is not a reason to
        # silently invert it.
        if stop is not None and (stop >= entry if long else stop <= entry):
            stop = None
        # A swing stop is the rule's risk definition, not a nicety: with none
        # available (too far, or on the wrong side) the setup is skipped.
        if stop is None and spec.stop is not None and spec.stop.kind == "swing":
            return Intent.nothing()
        target = spec.target.price(view, entry, long, stop=stop) if spec.target else None
        if target is not None and (target <= entry if long else target >= entry):
            target = None

        condition = spec.long_entry if long else spec.short_entry
        return Intent.enter(
            side,
            stop=stop,
            target=target,
            reason=condition.describe() if condition else "",
            trigger=spec.trigger,
        )

    def exit(self, view: View, position: Position) -> Intent:
        """The rule's own exit. Stops and targets are the engine's business.

        Evaluated per side, because "close the long" and "close the short" are
        usually different sentences - an exit that fires on a long would often be
        an entry signal for a short.
        """
        spec = self.spec
        # The session ending closes the position before any condition is read.
        # This is the whole difference between "I trade the London session" and
        # "I open trades during London and hold them through Tokyo".
        if (
            spec.close_outside_session
            and spec.sessions
            and view.at is not None
            and not in_any(spec.sessions, view.at)
        ):
            return Intent.exit("the session ended")

        condition = spec.long_exit if position.side is Side.LONG else spec.short_exit
        if condition is not None and condition.holds(view):
            return Intent.exit(condition.describe())
        return Intent.nothing()


def _reference(spec: StrategySpec, view: View, side: Side) -> float | None:
    """The price a stop or target should be measured from.

    Without a trigger that is the setup candle's close, because the fill will be
    the next open and the close is the best guess available when the decision is
    made.

    With one it is the level the order rests at, which is a much better answer: a
    stop "1% below entry" on a breakout means 1% below the breakout, not 1% below
    a close the trade never happened at. On a wide setup candle those are
    different trades.

    It is still the level rather than the fill, because the fill is not known
    until price gets there - a bar that gaps straight through fills worse, and
    the stop will sit slightly closer than asked. Erring that way is the right
    way to err.
    """
    if spec.trigger is None:
        return view.base.close
    field = spec.trigger.field if side is Side.LONG else spec.trigger.mirrored
    level = view.base.price(field, spec.trigger.ago)
    if level is None:
        return None
    return level * (1 + side.sign * spec.trigger.buffer_bps / 10_000.0)
