"""The loop: walk the session minute by minute, fill what was decided, let the
strategy decide again.

Per bar, in this order, and the order is the point:

  1. Orders decided on the previous close fill at this bar's **open**.
  2. Resting stops are tested against this bar's high and low. A stop that the
     open has already gapped through fills at the open, not at its trigger.
  3. The bar closes. The strategy sees it - and nothing later - and decides.

At the end of each day, legs expiring that day settle at intrinsic value against
the index's last close, and the day's equity is marked.

The engine never picks a strike, sets a rule or chooses an exit. Those are the
strategy's; this is the exchange and the ledger. That split is what lets one loop
serve an intraday straddle and a positional condor with adjustments alike.

What a fill cannot find a price for is not quietly dropped. An opening order with
no bar for five minutes is abandoned and counted; a closing order waits, because
walking away from a position is not an option the market offers either - at worst
it settles at expiry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import IntEnum
from typing import Literal, Protocol

from optbt.context import Context as DailyContext
from optbt.costs import TICK, Charges, CostModel
from optbt.market import Bar, OptionKey, View
from optbt.source import MarketSource

#: Bars an opening order may wait for a price before it is abandoned.
OPEN_ORDER_PATIENCE = 5


class Side(IntEnum):
    BUY = 1
    SELL = -1


@dataclass(frozen=True)
class Level:
    """A distance from a leg's fill: `pct` of the premium, or `points` of it."""

    kind: Literal["pct", "points"]
    value: float

    def against(self, fill: float, side: Side) -> float:
        """The price this far against a position: above a short, below a long."""
        move = fill * self.value if self.kind == "pct" else self.value
        return max(TICK, fill - side * move)

    def towards(self, fill: float, side: Side) -> float:
        """The price this far in a position's favour: below a short, above a long."""
        move = fill * self.value if self.kind == "pct" else self.value
        return max(TICK, fill + side * move)


@dataclass
class Leg:
    id: int
    key: OptionKey
    #: The position's direction: SELL is a short leg.
    side: Side
    lots: int
    lot_size: int
    tag: str
    entry_ts: datetime
    entry_price: float
    charges: Charges
    stop: float | None = None
    target: float | None = None
    exit_ts: datetime | None = None
    exit_price: float | None = None
    exit_reason: str | None = None

    @property
    def quantity(self) -> int:
        return self.lots * self.lot_size

    @property
    def is_open(self) -> bool:
        return self.exit_ts is None

    def pnl(self, mark: float) -> float:
        """Gross P&L against a price, before charges."""
        return self.side * (mark - self.entry_price) * self.quantity

    @property
    def realised(self) -> float:
        assert self.exit_price is not None
        return self.pnl(self.exit_price)


@dataclass
class Trade:
    """One structure, from its first leg opened to its last closed."""

    id: int
    opened: datetime
    legs: list[Leg] = field(default_factory=list)
    closed: datetime | None = None
    reason: str | None = None
    #: What happened, in order, in words - the replay of the trade.
    events: list[str] = field(default_factory=list)
    #: What the day looked like at entry - weekday, days to expiry, VIX, pivot
    #: zone and so on - set by the strategy, so results can be sliced by it.
    tags: dict[str, str | float | int | bool | None] = field(default_factory=dict)
    #: The lowest and highest the trade's gross P&L stood at any minute's close
    #: while it was open: how close it came to the stop, how much it gave back.
    worst: float = 0.0
    best: float = 0.0

    @property
    def gross(self) -> float:
        return sum(leg.realised for leg in self.legs if not leg.is_open)

    @property
    def charges(self) -> Charges:
        total = Charges()
        for leg in self.legs:
            total = total + leg.charges
        return total

    @property
    def net(self) -> float:
        return self.gross - self.charges.total


@dataclass
class _Order:
    key: OptionKey
    #: The order's direction, not the position's.
    side: Side
    lots: int
    tag: str
    reason: str
    closes: Leg | None = None
    stop: Level | None = None
    target: Level | None = None
    age: int = 0


class Context:
    """What a strategy acts through. Orders placed here fill on the next bar."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    @property
    def view(self) -> View:
        return self._engine.view

    @property
    def trade(self) -> Trade | None:
        return self._engine.trade

    @property
    def open_legs(self) -> list[Leg]:
        trade = self._engine.trade
        return [leg for leg in trade.legs if leg.is_open] if trade else []

    @property
    def pending(self) -> bool:
        return bool(self._engine.pending)

    @property
    def last_closed(self) -> Trade | None:
        """The trade that just finished, if the previous bar closed one.

        Read the same bar a position goes flat and no later - it is not a
        history, only what a strategy needs to decide whether to re-enter.
        """
        return self._engine.last_closed

    def open(
        self,
        key: OptionKey,
        side: Side,
        lots: int,
        tag: str,
        *,
        stop: Level | None = None,
        target: Level | None = None,
    ) -> None:
        """Open a leg. `stop` and `target` rest that far from its fill price."""
        self._engine.pending.append(
            _Order(key, side, lots, tag, "open", stop=stop, target=target)
        )

    def close(self, leg: Leg, reason: str) -> None:
        if leg.is_open and not any(o.closes is leg for o in self._engine.pending):
            side = Side.BUY if leg.side is Side.SELL else Side.SELL
            self._engine.pending.append(_Order(leg.key, side, leg.lots, leg.tag, reason, leg))

    def close_all(self, reason: str) -> None:
        for leg in self.open_legs:
            self.close(leg, reason)

    def set_stop(self, leg: Leg, price: float | None) -> None:
        leg.stop = price

    def mark(self, leg: Leg) -> float | None:
        """What the leg is worth now - see `View.mark`."""
        return self.view.mark(leg.key)

    def pnl(self) -> float | None:
        """The open trade's gross P&L, closed legs realised and open ones marked."""
        trade = self._engine.trade
        if trade is None:
            return 0.0
        total = 0.0
        for leg in trade.legs:
            if leg.is_open:
                mark = self.mark(leg)
                if mark is None:
                    return None
                total += leg.pnl(mark)
            else:
                total += leg.realised
        return total

    def skip(self, reason: str) -> None:
        """A day the strategy wanted to trade and could not. Counted, not hidden."""
        self._engine.skipped[reason] = self._engine.skipped.get(reason, 0) + 1

    def tag(self, **values: str | float | int | bool | None) -> None:
        """Describe the trade just opened. Applied when its first leg fills."""
        self._engine.pending_tags.update(values)

    def note(self, text: str) -> None:
        """Add to the open trade's log - or, with none open, to the log of the
        trade being opened, like `tag`."""
        line = f"{self.view.now:%Y-%m-%d %H:%M} {text}"
        if self._engine.trade is not None:
            self._engine.trade.events.append(line)
        else:
            self._engine.pending_notes.append(line)


class Strategy(Protocol):
    def on_day(self, ctx: Context) -> None: ...

    def on_bar(self, ctx: Context) -> None: ...


@dataclass
class Result:
    trades: list[Trade]
    #: Cumulative net P&L at each day's close, open legs marked.
    equity: list[tuple[date, float]]
    abandoned_orders: int
    days: int
    #: Days the strategy could not trade, by why.
    skipped: dict[str, int] = field(default_factory=dict)


class Engine:
    def __init__(
        self,
        history: MarketSource,
        strategy: Strategy,
        costs: CostModel | None = None,
    ) -> None:
        self.history = history
        self.strategy = strategy
        self.costs = costs or CostModel()
        self.pending: list[_Order] = []
        self.trade: Trade | None = None
        #: The trade that most recently went flat - a strategy's only way to
        #: notice a close happened, since `self.trade` is already None by then.
        self.last_closed: Trade | None = None
        self.view: View
        self._trades: list[Trade] = []
        self._next_id = 1
        self._abandoned = 0
        self.skipped: dict[str, int] = {}
        self.pending_tags: dict[str, str | float | int | bool | None] = {}
        self.pending_notes: list[str] = []
        self._banked = 0.0
        self._banked_upto = 0
        self._context: DailyContext | None = None

    @property
    def context(self) -> DailyContext:
        """Pivots, gap and VIX by day - built once, on first use."""
        if self._context is None:
            self._context = DailyContext(self.history)
        return self._context

    def run(self, start: date, end: date) -> Result:
        ctx = Context(self)
        equity: list[tuple[date, float]] = []
        days = self.history.trading_days(start, end)
        for day in days:
            bars = self.history.index_day(day)
            if not bars:
                continue
            self.view = View(self.history, day, bars, lambda: self.context)
            self.strategy.on_day(ctx)
            for i, bar in enumerate(bars):
                self._fill(day, bar)
                self._stops(day, bar)
                self.view._advance(i)
                self.strategy.on_bar(ctx)
                self._excursion(ctx)
            self._settle(day, bars[-1])
            equity.append((day, self._equity(ctx)))
        return Result(self._trades, equity, self._abandoned, len(days), self.skipped)

    # ---------------------------------------------------------------- fills

    def _bar(self, key: OptionKey, day: date, ts: datetime) -> Bar | None:
        return self.history.contract_day(key, day).get(ts)

    def _price(self, raw: float, side: Side) -> float:
        slip = self.costs.slip(raw)
        return raw + slip if side is Side.BUY else max(TICK, raw - slip)

    def _fill(self, day: date, bar: Bar) -> None:
        waiting: list[_Order] = []
        for order in self.pending:
            option = self._bar(order.key, day, bar.ts)
            if option is None:
                order.age += 1
                if order.closes is None and order.age >= OPEN_ORDER_PATIENCE:
                    self._abandoned += 1
                    if self.trade:
                        self.trade.events.append(f"{bar.ts:%Y-%m-%d %H:%M} abandoned {order.key}")
                else:
                    waiting.append(order)
                continue
            price = self._price(option.open, order.side)
            if order.closes is not None:
                self._exit(order.closes, bar.ts, price, order.reason, buy=order.side is Side.BUY)
            else:
                self._enter(order, day, bar.ts, price)
        self.pending = waiting
        self._maybe_close_trade(bar.ts)

    def _enter(self, order: _Order, day: date, ts: datetime, price: float) -> None:
        try:
            lot_size = self.history.lot_size(day, order.key.expiry)
        except ValueError:
            # Not guessed: a wrong lot mis-sizes the trade silently. Counted instead.
            self._abandoned += 1
            self.skipped["lot size unknown"] = self.skipped.get("lot size unknown", 0) + 1
            return
        if self.trade is None:
            self.trade = Trade(
                id=len(self._trades) + 1,
                opened=ts,
                tags=dict(self.pending_tags),
                events=list(self.pending_notes),
            )
            self.pending_tags.clear()
            self.pending_notes.clear()
            self._trades.append(self.trade)
        quantity = order.lots * lot_size
        leg = Leg(
            id=self._next_id,
            key=order.key,
            side=order.side,
            lots=order.lots,
            lot_size=lot_size,
            tag=order.tag,
            entry_ts=ts,
            entry_price=price,
            charges=self.costs.fill(day, price, quantity, buy=order.side is Side.BUY),
        )
        self._next_id += 1
        if order.stop is not None:
            leg.stop = order.stop.against(price, leg.side)
        if order.target is not None:
            leg.target = order.target.towards(price, leg.side)
        self.trade.legs.append(leg)
        verb = "sold" if leg.side is Side.SELL else "bought"
        self.trade.events.append(
            f"{ts:%Y-%m-%d %H:%M} {verb} {leg.lots}x{lot_size} {leg.key} @ {price:.2f}"
            + (f", stop {leg.stop:.2f}" if leg.stop else "")
            + (f", target {leg.target:.2f}" if leg.target else "")
        )

    def _exit(self, leg: Leg, ts: datetime, price: float, reason: str, *, buy: bool) -> None:
        leg.exit_ts, leg.exit_price, leg.exit_reason = ts, price, reason
        leg.charges = leg.charges + self.costs.fill(ts.date(), price, leg.quantity, buy=buy)
        if self.trade:
            self.trade.events.append(
                f"{ts:%Y-%m-%d %H:%M} closed {leg.key} @ {price:.2f} ({reason}), "
                f"leg {leg.realised:+,.0f}"
            )

    def _stops(self, day: date, bar: Bar) -> None:
        """Resting stops and targets, against this bar's range.

        A stop is a market order once touched: it fills at its trigger plus
        slippage, or at the open if the bar opened beyond it. A target is a
        limit: it fills at its own price, and only if the bar traded *through*
        it - a touch is not a fill, since a queue of others were there first.
        A bar that reached both is taken as having hit the stop, the worse one.
        """
        if self.trade is None:
            return
        for leg in self.trade.legs:
            if not leg.is_open or (leg.stop is None and leg.target is None):
                continue
            if any(o.closes is leg for o in self.pending):
                continue
            option = self._bar(leg.key, day, bar.ts)
            if option is None:
                continue
            short = leg.side is Side.SELL
            if leg.stop is not None and (
                option.high >= leg.stop if short else option.low <= leg.stop
            ):
                raw = max(leg.stop, option.open) if short else min(leg.stop, option.open)
                close_side = Side.BUY if short else Side.SELL
                self._exit(leg, bar.ts, self._price(raw, close_side), "stop", buy=short)
            elif leg.target is not None and (
                option.low < leg.target if short else option.high > leg.target
            ):
                raw = min(leg.target, option.open) if short else max(leg.target, option.open)
                self._exit(leg, bar.ts, raw, "target", buy=short)
        self._maybe_close_trade(bar.ts)

    def _settle(self, day: date, last: Bar) -> None:
        """Legs expiring today settle at intrinsic value against the index close.

        The exchange settles against a volume-weighted average of the last half
        hour; the 15:29 close stands in for it. A long leg in the money is
        exercised, and pays STT on its intrinsic value.

        "Today" means the last session on or before the expiry date, not only
        the date itself. The calendar lists 29 Jun 2023 - a holiday - beside the
        28th the contracts actually expired on, and a leg waiting for a session
        that never comes would stay open for ever.
        """
        if self.trade is None:
            return
        spot = last.close
        following = self.history.next_trading_day(day)
        for leg in self.trade.legs:
            due = leg.key.expiry <= day or (following is not None and following > leg.key.expiry)
            if leg.is_open and due:
                value = leg.key.intrinsic(spot)
                self.pending = [o for o in self.pending if o.closes is not leg]
                leg.exit_ts, leg.exit_price, leg.exit_reason = last.ts, value, "expiry"
                if leg.side is Side.BUY and value > 0:
                    leg.charges = leg.charges + self.costs.exercise(day, value, leg.quantity)
                self.trade.events.append(
                    f"{last.ts:%Y-%m-%d %H:%M} settled {leg.key} at {value:.2f} "
                    f"(spot {spot:.2f}), leg {leg.realised:+,.0f}"
                )
        self._maybe_close_trade(last.ts)

    def _maybe_close_trade(self, ts: datetime) -> None:
        trade = self.trade
        if trade is None or any(leg.is_open for leg in trade.legs):
            return
        if any(o.closes is None for o in self.pending):
            return  # a roll: the old legs are closed, the new ones not yet filled
        trade.closed = ts
        reasons = [leg.exit_reason for leg in trade.legs if leg.exit_ts == ts]
        trade.reason = reasons[-1] if reasons else None
        self.trade = None
        self.last_closed = trade

    def _excursion(self, ctx: Context) -> None:
        trade = self.trade
        if trade is None or not trade.legs:
            return
        pnl = ctx.pnl()
        if pnl is not None:
            trade.worst = min(trade.worst, pnl)
            trade.best = max(trade.best, pnl)

    def _equity(self, ctx: Context) -> float:
        # Closed trades are summed once each, as they close, not recounted daily.
        while self._banked_upto < len(self._trades) and self._trades[self._banked_upto].closed:
            self._banked += self._trades[self._banked_upto].net
            self._banked_upto += 1
        realised = self._banked + sum(
            t.net for t in self._trades[self._banked_upto :] if t.closed is not None
        )
        if self.trade is None:
            return realised
        open_pnl = ctx.pnl() or 0.0
        return realised + open_pnl - self.trade.charges.total
