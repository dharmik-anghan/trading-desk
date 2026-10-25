"""The run: bars in, trades out.

Everything here is about not flattering the result. The loop itself is twenty
lines; the rest is the difference between a backtest and a sales pitch.

**A decision is made on a close and filled on the next open.** A rule reads bar i
after it has closed, so the earliest it could have acted is bar i+1, and the first
price available then is that bar's open. Filling at the close that produced the
signal is the commonest way a backtest invents money, because it buys at the price
that caused the decision.

**A bar cannot say what happened inside it.** If a bar's low reaches the stop and
its high reaches the target, one of them came first and the bar does not record
which. This always assumes the stop. Pessimistic on purpose: the alternative is a
result that quietly assumes the best of every ambiguous bar, and ambiguous bars
are exactly the volatile ones where it matters.

**Liquidation is checked before anything else.** A leveraged position that went
past its liquidation price during a bar was closed by the venue, whatever the rule
wanted. Checking the stop first would report a loss the trader chose instead of one
the exchange imposed.

**Costs are charged, not netted off at the end.** Fees at both fills, funding at
each settlement crossed, slippage on every fill. They come out of the equity curve
as they are incurred, so a drawdown is a real one.

The engine holds one position at a time. That is a real restriction and a
deliberate one: position sizing, pyramiding and portfolio effects are a separate
problem, and mixing them in here would make it impossible to tell whether a result
came from the idea or from the sizing.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from backtest.market import Costs, Market, Side
from backtest.models import Action, Exit, Intent, Position, Trade
from backtest.resample import closes_at
from backtest.view import Shared, View, build, wind
from marketdata.models import Bar, Interval

log = logging.getLogger(__name__)


class Rule(Protocol):
    """A strategy.

    Two methods and no state that outlives a run. It is handed what was knowable
    and returns what it wants; it never sees the engine, the market or the money,
    which is why the same object can later be pointed at a live feed.
    """

    name: str
    #: Timeframes above the traded one this rule reads. Declared so the engine can
    #: align them, and so asking for one that was not declared is an error rather
    #: than a silent lookahead.
    context: tuple[Interval, ...]

    def entry(self, view: View) -> Intent:
        """Called on every closed bar while flat."""
        ...

    def exit(self, view: View, position: Position) -> Intent:
        """Called on every closed bar while in a position."""
        ...


@dataclass
class Armed:
    """An order resting at a level, waiting for price to reach it.

    Held across bars, which is the whole point: the setup happened on one candle
    and the trade may happen on a later one, or not at all.
    """

    intent: Intent
    side: Side
    level: float
    #: The bar index the order stops resting after.
    until: int
    reason: str


@dataclass(frozen=True)
class Execution:
    """The assumptions a fill is made under.

    Written down rather than hardcoded because they are assumptions, and a result
    should be reproducible from the numbers that produced it.
    """

    #: Money to trade with, in the market's quote currency.
    capital: float = 1000.0
    #: How a position is sized. Three answers, because they are three different
    #: questions and conflating them hides which one a result depends on:
    #:
    #:   "equity"   a fraction of what the account is worth now, so the run
    #:              compounds and a losing run trades smaller
    #:   "quantity" a fixed number of contracts every time - the lot you would
    #:              actually type into the ticket
    #:   "notional" a fixed amount of money at work, whatever the account is
    #:
    #: Compounding flatters a rule that worked early and punishes one that
    #: worked late, which is why a fixed size is often the more honest test.
    sizing: str = "equity"
    #: For "equity": the fraction committed as margin per trade.
    risk: float = 1.0
    #: For "quantity": contracts per trade, in the instrument's own units.
    quantity: float = 0.0
    #: For "notional": the position's value in the quote currency.
    notional: float = 0.0
    leverage: float = 1.0
    #: How far the fill lands from the price it was decided at, in basis points,
    #: always against you. Not a guess that can be checked, which is why it is a
    #: dial: at 5m on a liquid pair a basis point or two is generous, and a result
    #: that only works at zero is not a result.
    slippage_bps: float = 1.0
    #: Enter with a resting limit order at the price the decision was made at,
    #: rather than taking the next open. Halves the entry fee - 0.019% against
    #: 0.047% - and is a much stronger claim, so it is modelled rather than
    #: assumed: the order fills only if the next bar trades *through* the limit,
    #: not merely touches it. Touching it means the order was at the back of a
    #: queue that may never have been reached, and a rule that only works when
    #: every touch fills is a rule that only works on paper.
    #:
    #: Exits stay taker whatever this says. A stop is a market order by nature,
    #: and a target that has to be filled cannot wait for a queue.
    maker_entry: bool = False


@dataclass
class Result:
    """What a run produced."""

    rule: str
    symbol: str
    interval: Interval
    started: datetime | None = None
    ended: datetime | None = None
    bars: int = 0
    capital: float = 0.0
    trades: list[Trade] = field(default_factory=list)
    #: Equity after every bar, for the curve. Same length as the bars traded.
    equity: list[float] = field(default_factory=list)
    #: Anything the reader has to know to judge the numbers - a funding proxy, a
    #: position still open at the end, a window shorter than the warm-up.
    caveats: list[str] = field(default_factory=list)
    #: Signals that could not be taken: below the venue's minimum, or more than
    #: the account could post margin for. Counted rather than ignored, because a
    #: rule whose trades were mostly unaffordable did not really return what the
    #: curve says.
    skipped_too_small: int = 0
    skipped_unaffordable: int = 0
    #: Positions closed and immediately opened the other way on the same bar.
    reversals: int = 0
    #: Setups that armed an order, and those whose order was never reached
    #: before it expired. The ratio is worth as much as the win rate: a rule
    #: that arms five hundred times and fills eighty is a different animal from
    #: one that fills four hundred, whatever the trades look like afterwards.
    armed: int = 0
    expired_unfilled: int = 0

    @property
    def final(self) -> float:
        return self.equity[-1] if self.equity else self.capital


def run(
    bars: Sequence[Bar],
    rule: Rule,
    market: Market,
    *,
    interval: Interval,
    execution: Execution | None = None,
    shared: Shared | None = None,
) -> Result:
    """Walk the bars once, giving the rule only what had happened.

    `shared` lets a study that runs many rules over the same bars keep the
    resampled frames and indicator arrays between runs. It changes how long a run
    takes and nothing about what it decides: the view is built from the same bars
    either way, and `tests/backtest/test_study.py` holds the two to identical
    trades.
    """
    settings = execution or Execution()
    result = Result(
        rule=rule.name,
        symbol=market.symbol,
        interval=interval,
        capital=settings.capital,
        bars=len(bars),
    )
    if not bars:
        result.caveats.append("No bars, so nothing was tested")
        return result

    result.started = bars[0].ts
    result.ended = closes_at(bars[-1].ts, interval)

    view, cursors = build(bars, interval, rule.context, shared)
    equity = settings.capital
    position: Position | None = None
    #: What was decided at the previous bar's close, waiting to be filled at this
    #: bar's open. The one-bar delay, made physical.
    #:
    #: Two of them, because closing and opening the other way on the same signal
    #: is one decision. Without that the engine cannot turn round: the signal
    #: that closes a short is usually the same signal that opens a long, and if
    #: the entry is only considered after the exit has been filled then by the
    #: next bar the crossing has passed and the trade is never taken. A
    #: long-and-short crossover ran for three years and took shorts only.
    pending_exit: Intent | None = None
    pending_enter: Intent | None = None
    #: The price a resting entry would sit at, when entering as a maker.
    pending_limit: float | None = None
    #: An order waiting for price to come to it, from a setup on an earlier bar.
    armed: Armed | None = None

    for i, bar in enumerate(bars):
        # --- act on what was decided last bar, at this bar's open -----------
        reversing = pending_exit is not None and pending_enter is not None
        if pending_exit is not None and position is not None:
            equity, trade = _close(
                position, bar.open, bar.ts, Exit.RULE, pending_exit.reason, market, settings,
                equity,
            )
            result.trades.append(trade)
            position = None
        if pending_enter is not None and position is None:
            position, why_not = _open(
                pending_enter, bar, market, settings, equity, limit=pending_limit
            )
            if why_not == "small":
                result.skipped_too_small += 1
            elif why_not == "afford":
                result.skipped_unaffordable += 1
            elif reversing and position is not None:
                result.reversals += 1
        pending_exit = None
        pending_enter = None
        pending_limit = None

        # --- an order left resting from an earlier setup --------------------
        #
        # Checked after a pending exit has been filled and before the bar is
        # judged against an open position, so a setup can trigger on the same bar
        # a position was closed on - which is what a stop-and-reverse looks like
        # when the reverse is a breakout rather than an immediate flip.
        if armed is not None and position is None:
            reached = _reached(armed, bar)
            if reached is not None:
                position, why_not = _open(
                    armed.intent, bar, market, settings, equity, at=reached
                )
                if why_not == "small":
                    result.skipped_too_small += 1
                elif why_not == "afford":
                    result.skipped_unaffordable += 1
                armed = None
            elif i >= armed.until:
                result.expired_unfilled += 1
                armed = None

        # --- what the bar did to an open position ---------------------------
        if position is not None:
            position = _carry(position, bar, market, interval)
            ending = _ended_during(position, bar, market)
            if ending is not None:
                price, why = ending
                equity, trade = _close(
                    position, price, bar.ts, why, _said(why), market, settings, equity
                )
                result.trades.append(trade)
                position = None

        # --- decide, on this bar's close ------------------------------------
        wind(view, cursors, i)
        if position is None:
            intent = rule.entry(view)
            if intent.action is Action.ENTER and intent.trigger is not None:
                # A setup, not a trade. Arm an order and wait for price. A newer
                # setup replaces an older one rather than queueing behind it:
                # two orders at two levels is a different strategy from the one
                # that was written.
                resting = _arm(intent, view, i, settings)
                if resting is not None:
                    if armed is None:
                        result.armed += 1
                    armed = resting
                pending_enter = None
            else:
                pending_enter = intent if intent.action is Action.ENTER else None
        else:
            leaving = rule.exit(view, position)
            if leaving.action is Action.EXIT:
                pending_exit = leaving
                # And, on the same bar, whether the rule wants the other side.
                # Only the other side: an entry agreeing with the position we are
                # closing is a contradiction, and re-entering what was just exited
                # would be churn the strategy did not ask for.
                turning = rule.entry(view)
                if turning.action is Action.ENTER and turning.side is position.side.opposite:
                    if turning.trigger is not None:
                        resting = _arm(turning, view, i, settings)
                        if resting is not None:
                            if armed is None:
                                result.armed += 1
                            armed = resting
                    else:
                        pending_enter = turning
        pending_limit = bar.close if (pending_enter and settings.maker_entry) else None

        result.equity.append(_equity_now(equity, position, bar.close, market))

    if result.expired_unfilled:
        result.caveats.append(
            f"{result.expired_unfilled:,} setups armed an order that price never reached, "
            "so they cost nothing and are not trades"
        )
    if result.skipped_too_small:
        result.caveats.append(
            f"{result.skipped_too_small:,} signals were below the venue's minimum size and "
            "were not taken"
        )
    if result.skipped_unaffordable:
        result.caveats.append(
            f"{result.skipped_unaffordable:,} signals needed more margin than the account "
            "had and were not taken"
        )

    if position is not None:
        last = bars[-1]
        equity, trade = _close(
            position,
            last.close,
            last.ts,
            Exit.END_OF_DATA,
            _said(Exit.END_OF_DATA),
            market,
            settings,
            equity,
        )
        result.trades.append(trade)
        result.equity[-1] = equity
        result.caveats.append(
            "A position was still open when the data ended; it is closed at the last "
            "price, which is a price nobody chose"
        )

    return result


def _said(why: Exit) -> str:
    """What to write against a close nobody's condition asked for."""
    return {
        Exit.STOP: "the stop was reached",
        Exit.TARGET: "the target was reached",
        Exit.LIQUIDATION: "the venue closed it",
        Exit.END_OF_DATA: "the data ran out",
    }.get(why, "")


def _arm(intent: Intent, view: View, i: int, settings: Execution) -> Armed | None:
    """Work out the level a setup rests its order at.

    Read off the setup candle through the view, so it is a closed bar's price and
    nothing later. Returns None when that candle is not there yet - a trigger
    that looks three bars back at bar one has nothing to aim at.
    """
    trigger = intent.trigger
    side = intent.side
    if trigger is None or side is None:
        return None

    field = trigger.field if side is Side.LONG else trigger.mirrored
    level = view.base.price(field, trigger.ago)
    if level is None or level <= 0:
        return None

    # The cushion goes the way the break does: above for a long, below for a
    # short. A buffer that made the order easier to fill would be a discount on
    # the very thing being tested.
    level *= 1 + side.sign * trigger.buffer_bps / 10_000.0
    return Armed(
        intent=intent,
        side=side,
        level=level,
        until=i + max(1, trigger.within),
        reason=intent.reason,
    )


def _reached(armed: Armed, bar: Bar) -> float | None:
    """Whether this bar reached the resting order, and at what price it filled.

    Gaps are the part worth getting right. A buy-stop above the market fills at
    its level when price trades up through it, but when the bar *opens* beyond
    the level it fills at the open - worse, sometimes far worse. Filling a gap at
    the level would hand the strategy the whole gap for nothing, on exactly the
    bars where the move was biggest.
    """
    if armed.side is Side.LONG:
        if bar.open >= armed.level:
            return bar.open
        return armed.level if bar.high >= armed.level else None
    if bar.open <= armed.level:
        return bar.open
    return armed.level if bar.low <= armed.level else None


def _slipped(price: float, side: Side, settings: Execution, *, opening: bool) -> float:
    """The fill, moved against the trade.

    Against in both directions: a long pays more to get in and receives less to get
    out. Slippage that helped would not be slippage.
    """
    drift = price * settings.slippage_bps / 10_000.0
    worse = side.sign if opening else -side.sign
    return price + drift * worse


def _open(
    intent: Intent,
    bar: Bar,
    market: Market,
    settings: Execution,
    equity: float,
    *,
    limit: float | None = None,
    at: float | None = None,
) -> tuple[Position | None, str]:
    """Open a position, or say why not.

    The second half of the pair matters as much as the first. A signal that could
    not be taken - below the venue's minimum, or more margin than the account had
    - is not the same as a signal that did not happen, and counting them is what
    stops a curve describing trades nobody could have placed.
    """
    side = intent.side
    if side is None:
        return None, "none"

    if at is not None:
        # A triggered order is a market order the moment it is reached, so it
        # pays slippage and the taker fee whatever the entry style says.
        price = _slipped(at, side, settings, opening=True)
    elif limit is None:
        price = _slipped(bar.open, side, settings, opening=True)
    else:
        # A resting order fills at its own price and no worse, so no slippage -
        # but only if the bar traded through it. Strictly through: a bar that
        # merely reached the limit says nothing about whether the queue in front
        # of it cleared.
        through = bar.low < limit if side is Side.LONG else bar.high > limit
        if not through:
            return None, "unfilled"
        price = limit
    if price <= 0:
        return None, "none"

    quantity = _size(market, settings, price, equity)
    if quantity <= 0:
        return None, "none"
    if quantity < market.min_quantity or market.notional(quantity, price) < market.min_notional:
        return None, "small"
    if market.margin(quantity, price, settings.leverage) > max(0.0, equity):
        return None, "afford"

    fee = market.fee(
        quantity, price, maker=limit is not None and at is None, opening=True, side=side
    )
    slippage = abs(price - (at if at is not None else bar.open)) * quantity * market.multiplier
    return Position(
        side=side,
        quantity=quantity,
        entry=price,
        opened_at=bar.ts,
        stop=intent.stop,
        target=intent.target,
        liquidation=market.liquidation(side, price, settings.leverage),
        reason=intent.reason,
        costs=Costs(fees=fee, slippage=slippage),
        carried_to=bar.ts,
    ), "opened"


def _size(market: Market, settings: Execution, price: float, equity: float) -> float:
    """How many contracts, by whichever rule was chosen.

    Rounded down to the venue's precision. Down rather than to nearest, because
    rounding up can put an order above the margin just checked for it, and the
    cost of being one step small is a rounding error while the cost of being one
    step large is a rejected order.
    """
    if settings.sizing == "quantity":
        wanted = settings.quantity
    elif settings.sizing == "notional":
        wanted = settings.notional / (price * market.multiplier)
    else:
        # A fraction of what the account is worth now, so the run compounds and a
        # losing run trades smaller - which is what an account actually does.
        committed = max(0.0, equity) * settings.risk
        wanted = committed * settings.leverage / (price * market.multiplier)
    return market.round_quantity(wanted)


def _carry(position: Position, bar: Bar, market: Market, interval: Interval) -> Position:
    """Charge whatever holding cost accrued over this bar."""
    frm = position.carried_to or position.opened_at
    to = closes_at(bar.ts, interval)
    if to <= frm:
        return position
    cost = market.carry(position.side, position.quantity, frm, to, bar.close)
    position.costs = position.costs.plus(funding=cost)
    position.carried_to = to
    return position


def _ended_during(position: Position, bar: Bar, market: Market) -> tuple[float, Exit] | None:
    """Whether this bar closed the position, and at what price.

    Order matters and is the honest one: liquidation, then stop, then target. A
    bar that reached all three is reported as the worst of them, because a bar
    cannot say which came first and assuming otherwise is assuming profit.
    """
    low, high = bar.low, bar.high

    if position.liquidation is not None:
        hit = low <= position.liquidation if position.side is Side.LONG else (
            high >= position.liquidation
        )
        if hit:
            return position.liquidation, Exit.LIQUIDATION

    if position.stop is not None:
        hit = low <= position.stop if position.side is Side.LONG else high >= position.stop
        if hit:
            return position.stop, Exit.STOP

    if position.target is not None:
        hit = high >= position.target if position.side is Side.LONG else low <= position.target
        if hit:
            return position.target, Exit.TARGET

    return None


def _close(
    position: Position,
    price: float,
    at: datetime,
    why: Exit,
    exit_reason: str,
    market: Market,
    settings: Execution,
    equity: float,
) -> tuple[float, Trade]:
    """Close, charge the exit, and hand back the new equity."""
    # A liquidation is not a price anyone chose, so no slippage is added to it -
    # the venue closed the position at that level by definition.
    filled = price if why is Exit.LIQUIDATION else _slipped(
        price, position.side, settings, opening=False
    )
    multiplier = market.multiplier
    # Always taker on the way out: a stop is a market order and a target that
    # must fill cannot sit in a queue.
    fee = market.fee(
        position.quantity, filled, maker=False, opening=False, side=position.side
    )
    slippage = abs(filled - price) * position.quantity * multiplier
    costs = position.costs.plus(fees=fee, slippage=slippage)
    gross = position.gross_at(filled, multiplier)

    trade = Trade(
        side=position.side,
        quantity=position.quantity,
        opened_at=position.opened_at,
        closed_at=at,
        entry=position.entry,
        exit_price=filled,
        why=why,
        entry_reason=position.reason,
        exit_reason=exit_reason,
        costs=costs,
        gross=gross,
    )
    return equity + trade.net, trade


def _equity_now(equity: float, position: Position | None, price: float, market: Market) -> float:
    """Equity marked to the current price, so the curve shows an open loss.

    A curve that only moved when a trade closed would hide every drawdown taken
    inside a position, which is most of the drawdown there ever is.
    """
    if position is None:
        return equity
    multiplier = market.multiplier
    return equity + position.gross_at(price, multiplier) - position.costs.total
