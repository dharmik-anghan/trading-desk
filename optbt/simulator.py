"""The option market replayed at any minute of the store, to trade by hand.

A moment is a bar: "10:53" is the market as it stood when the 10:53 bar
closed. Everything shown at a moment - the chain, a position's price, the
payoff - is from that bar or before it, and a leg added then fills at what the
contract last traded for. Stepping forward is how time passes: a leg's resting
stop or target is tested against every bar the step went over, the way the
backtest engine tests them, and a leg still open past its expiry settles at
intrinsic value against the index's close that session.

A leg is history-independent: it knows when it was entered and, once it has
been, when it left. What it is at a moment - not yet entered, open, or closed -
follows from that, so stepping back before an entry shows the leg as pending
rather than losing it.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Literal

from analytics import black_scholes as bs
from analytics import payoff as pay
from broker.models import OptionType
from optbt.costs import CostModel
from optbt.data.history import ChainQuote, History
from optbt.data.models import Kind
from optbt.margin import MarginLeg, estimate
from optbt.market import SESSION_OPEN, Bar, OptionKey
from optbt.marks import implied_vol, intrinsic, years_to
from venues.instruments import INDIA_VIX

#: Where a fresh simulation opens: five minutes into the session.
DEFAULT_CLOCK = (9, 20)

#: Expiries offered at a moment, nearest first.
EXPIRIES_SHOWN = 8

_MOVE = re.compile(r"^([+-])(\d+)([mhd])$")


def _ot(kind: Kind) -> OptionType:
    return "CE" if kind is Kind.CALL else "PE"


def _decision(at: datetime) -> datetime:
    """When the bar named `at` closes - the moment its prices are known."""
    return at + timedelta(minutes=1)


# ------------------------------------------------------------------- time


class Clock:
    """Moving through the store's sessions a bar at a time."""

    def __init__(self, h: History) -> None:
        self.h = h
        self.days = h.trading_days(date(2000, 1, 1), date(2100, 1, 1))
        if not self.days:
            raise ValueError(f"no {h.underlying} history in the store")

    def bars(self, day: date) -> list[datetime]:
        return [b.ts for b in self.h.index_day(day)]

    def _day_index(self, day: date) -> int:
        """The session on `day`, or the next one after it (the last, past the end)."""
        for i, d in enumerate(self.days):
            if d >= day:
                return i
        return len(self.days) - 1

    def snap(self, at: datetime | None) -> datetime:
        """The bar a moment means: the last one at or before it that session."""
        if at is None:
            last = self.days[-1]
            target = datetime.combine(last, SESSION_OPEN).replace(
                hour=DEFAULT_CLOCK[0], minute=DEFAULT_CLOCK[1]
            )
            return self.snap(target)
        i = self._day_index(at.date())
        day = self.days[i]
        bars = self.bars(day)
        if day != at.date():
            return bars[0]
        earlier = [ts for ts in bars if ts <= at]
        return earlier[-1] if earlier else bars[0]

    def move(self, at: datetime, move: str) -> datetime:
        """`at`, moved: "sod"/"eod", or "+5m", "-1h", "+1d"."""
        at = self.snap(at)
        i = self._day_index(at.date())
        bars = self.bars(self.days[i])
        if move == "sod":
            return bars[0]
        if move == "eod":
            return bars[-1]
        m = _MOVE.match(move)
        if not m:
            raise ValueError(f"not a move: {move!r}")
        sign = 1 if m.group(1) == "+" else -1
        n = int(m.group(2))
        if m.group(3) == "d":
            j = min(max(i + sign * n, 0), len(self.days) - 1)
            day = self.days[j]
            return self.snap(datetime.combine(day, at.time()))
        minutes = n * (60 if m.group(3) == "h" else 1)
        pos = bars.index(at) + sign * minutes
        # Carried across sessions bar by bar, so +1h at 15:00 lands at 09:44 the
        # next session rather than at a clock time that does not trade.
        while pos >= len(bars):
            if i == len(self.days) - 1:
                return bars[-1]
            pos -= len(bars)
            i += 1
            bars = self.bars(self.days[i])
        while pos < 0:
            if i == 0:
                return bars[0]
            i -= 1
            bars = self.bars(self.days[i])
            pos += len(bars)
        return bars[pos]

    def expiry_close(self, expiry: date) -> datetime | None:
        """The last bar of the session a contract settles in, if it is in the store."""
        sessions = [d for d in self.days if d <= expiry]
        if not sessions or (self.days[-1] < expiry):
            return None
        bars = self.bars(sessions[-1])
        return bars[-1] if bars else None


# ------------------------------------------------------------------ chain


@dataclass(frozen=True)
class ChainSide:
    """One option of a strike, as of the moment."""

    ltp: float
    last_at: datetime
    oi: int
    volume: int
    iv: float | None
    delta: float | None


@dataclass(frozen=True)
class ChainRow:
    strike: float
    ce: ChainSide | None
    pe: ChainSide | None


def forward_of(quotes: list[ChainQuote], spot: float) -> float:
    """The forward the chain prices to, by put-call parity at the strike nearest
    spot that has both a call and a put - spot itself if none does."""
    calls = {q.key.strike: q.price for q in quotes if q.key.kind is Kind.CALL}
    puts = {q.key.strike: q.price for q in quotes if q.key.kind is Kind.PUT}
    both = calls.keys() & puts.keys()
    if not both:
        return spot
    k = min(both, key=lambda s: abs(s - spot))
    return calls[k] - puts[k] + k


def chain(h: History, expiry: date, at: datetime, spot: float) -> tuple[list[ChainRow], float]:
    """The chain of one expiry at a moment, with each option's IV and delta
    solved from its own price against the chain's forward."""
    quotes = h.chain_asof(expiry, at)
    forward = forward_of(quotes, spot)
    years = years_to(expiry, _decision(at))
    rows: dict[float, dict[str, ChainSide]] = {}
    for q in quotes:
        ot = _ot(q.key.kind)
        sigma = implied_vol(q.price, forward, q.key.strike, years, ot) if years > 0 else None
        if sigma is not None:
            delta = bs.greeks(forward, q.key.strike, 0.0, sigma, years, ot).delta
        else:
            # No time value to read a volatility from: in the money it moves
            # with the index one for one, out of it not at all.
            itm = intrinsic(forward, q.key.strike, ot) > 0
            delta = (1.0 if ot == "CE" else -1.0) if itm else 0.0
        rows.setdefault(q.key.strike, {})[ot] = ChainSide(
            q.price, q.last_at, q.oi, q.volume, sigma, delta
        )
    out = [ChainRow(k, v.get("CE"), v.get("PE")) for k, v in sorted(rows.items())]
    return out, forward


# ------------------------------------------------------------------- legs


@dataclass(frozen=True)
class SimLeg:
    id: str
    side: Literal["buy", "sell"]
    kind: Kind
    strike: float
    expiry: date
    lots: int
    entry_at: datetime
    #: None: fill at the contract's price as of `entry_at`.
    entry_price: float | None = None
    #: Resting levels, as option prices.
    stop: float | None = None
    target: float | None = None
    exit_at: datetime | None = None
    #: None with `exit_at` set: fill at the contract's price as of `exit_at`.
    exit_price: float | None = None
    exit_reason: str | None = None
    #: Out of the payoff and totals, but still marked and still triggered.
    enabled: bool = True

    @property
    def key(self) -> OptionKey:
        return OptionKey(self.expiry, self.strike, self.kind)

    @property
    def sign(self) -> int:
        return 1 if self.side == "buy" else -1


@dataclass(frozen=True)
class LegState:
    leg: SimLeg
    status: Literal["pending", "open", "closed", "error"]
    lot_size: int | None = None
    ltp: float | None = None
    ltp_at: datetime | None = None
    iv: float | None = None
    error: str | None = None
    #: What the leg costs beyond its premium: charges on its fills and slippage,
    #: and for an open leg what closing it at its last price would add.
    charges: float = 0.0
    slippage: float = 0.0


@dataclass(frozen=True)
class Exit:
    """The whole position squared off by its P&L stop or target."""

    reason: Literal["portfolio stop", "portfolio target"]
    at: datetime
    #: Net P&L when it was hit.
    net: float


@dataclass(frozen=True)
class Rule:
    """Square everything off when the net P&L of the included legs reaches these, in rupees."""

    stop: float | None = None
    target: float | None = None

    @property
    def active(self) -> bool:
        return self.stop is not None or self.target is not None


def leg_costs(
    cm: CostModel, leg: SimLeg, qty: int, mark: float | None, today: date
) -> tuple[float, float]:
    """Charges and slippage on a leg's fills: its entry, its exit - or, still
    open, an exit at `mark` today. Slippage is counted as a cost of each fill
    rather than moving the price shown, so a fill still reads as the market's."""
    if leg.entry_price is None or qty <= 0:
        return 0.0, 0.0
    buy = leg.side == "buy"
    slip = cm.slip(leg.entry_price) * qty
    total = cm.fill(leg.entry_at.date(), leg.entry_price, qty, buy=buy).total
    if leg.exit_at is not None and leg.exit_price is not None:
        if leg.exit_reason == "expiry":
            # Settled, not traded: no order and no slippage. A long in the money
            # is exercised and pays STT on its intrinsic value.
            if buy and leg.exit_price > 0:
                total += cm.exercise(leg.exit_at.date(), leg.exit_price, qty).total
        else:
            slip += cm.slip(leg.exit_price) * qty
            total += cm.fill(leg.exit_at.date(), leg.exit_price, qty, buy=not buy).total
    elif mark is not None:
        slip += cm.slip(mark) * qty
        total += cm.fill(today, mark, qty, buy=not buy).total
    return total + slip, slip


def _touch(leg: SimLeg, bar: Bar, ts: datetime) -> SimLeg | None:
    """The leg closed by its stop or target on this bar, or None.

    The backtest engine's fills: a stop is a market order once touched, filling
    at its level or at the open of a bar that gapped through it; a target is a
    limit, filling only on a bar that traded through it. A bar that reached
    both is taken as the stop.
    """
    short = leg.side == "sell"
    if leg.stop is not None and (bar.high >= leg.stop if short else bar.low <= leg.stop):
        price = max(leg.stop, bar.open) if short else min(leg.stop, bar.open)
        return replace(leg, exit_at=ts, exit_price=price, exit_reason="stop")
    if leg.target is not None and (bar.low < leg.target if short else bar.high > leg.target):
        price = min(leg.target, bar.open) if short else max(leg.target, bar.open)
        return replace(leg, exit_at=ts, exit_price=price, exit_reason="target")
    return None


def _scan(clock: Clock, leg: SimLeg, after: datetime, upto: datetime) -> SimLeg:
    """A leg's stop and target against every bar of the contract in (after, upto],
    then settlement if its expiry closed inside that span. See `_touch`."""
    if leg.exit_at is not None or leg.entry_price is None:
        return leg
    h = clock.h
    settles = clock.expiry_close(leg.expiry)
    if leg.stop is not None or leg.target is not None:
        days = [d for d in clock.days if after.date() <= d <= upto.date()]
        for d in days:
            for ts, bar in sorted(h.contract_day(leg.key, d).items()):
                if ts <= after or ts > upto or (settles is not None and ts > settles):
                    continue
                closed = _touch(leg, bar, ts)
                if closed is not None:
                    return closed
    return _settle(clock, leg, upto)


def _walk(
    clock: Clock,
    legs: list[tuple[SimLeg, int]],
    after: datetime,
    upto: datetime,
    rule: Rule,
    multiplier: int,
    cm: CostModel,
) -> tuple[list[SimLeg], Exit | None]:
    """Minute by minute through (after, upto]: each leg's own stop and target,
    its expiry, then the whole position's net P&L against `rule`.

    When the rule is met, every included leg still open is closed at its last
    price that minute - the price the P&L was judged on.
    """
    h = clock.h
    out = [leg for leg, _ in legs]
    lots = [lot for _, lot in legs]
    qty = [leg.lots * lot * multiplier for leg, lot in legs]
    last: list[float | None] = []
    for leg in out:
        found = h.price_asof(leg.key, max(after, leg.entry_at))
        last.append(found[1] if found else leg.entry_price)
    settles = [clock.expiry_close(leg.expiry) for leg in out]
    days = [d for d in clock.days if after.date() <= d <= upto.date()]
    for d in days:
        bars = [h.contract_day(leg.key, d) for leg in out]
        for ts in clock.bars(d):
            if ts <= after or ts > upto:
                continue
            for i, leg in enumerate(out):
                if leg.exit_at is not None or leg.entry_price is None or ts <= leg.entry_at:
                    continue
                bar = bars[i].get(ts)
                if bar is not None:
                    last[i] = bar.close
                    closed = _touch(leg, bar, ts)
                    if closed is not None:
                        out[i] = closed
                        continue
                if settles[i] == ts:
                    out[i] = _settle(clock, leg, ts)
            net = 0.0
            for i, leg in enumerate(out):
                if not leg.enabled or leg.entry_price is None or ts < leg.entry_at:
                    continue
                gone = leg.exit_at is not None and leg.exit_price is not None
                mark = leg.exit_price if gone else last[i]
                if mark is None:
                    continue
                net += leg.sign * (mark - leg.entry_price) * qty[i]
                net -= leg_costs(cm, leg, qty[i], None if gone else mark, ts.date())[0]
            hit: Literal["portfolio stop", "portfolio target"] | None = None
            if rule.stop is not None and net <= -rule.stop:
                hit = "portfolio stop"
            elif rule.target is not None and net >= rule.target:
                hit = "portfolio target"
            if hit is not None:
                for i, leg in enumerate(out):
                    if leg.enabled and leg.exit_at is None and leg.entry_at < ts:
                        price = last[i] if last[i] is not None else leg.entry_price
                        out[i] = replace(leg, exit_at=ts, exit_price=price, exit_reason=hit)
                return out, Exit(hit, ts, net)
    del lots
    return [_settle(clock, leg, upto) for leg in out], None


def _settle(clock: Clock, leg: SimLeg, upto: datetime) -> SimLeg:
    """A leg still open when its expiry session closed, by `upto`, settles at
    intrinsic value against the index's close then."""
    settles = clock.expiry_close(leg.expiry)
    if leg.exit_at is not None or settles is None or not leg.entry_at < settles <= upto:
        return leg
    spot = clock.h.close_at(clock.h.index_symbol, settles)
    if spot is None:
        return leg
    value = intrinsic(spot, leg.strike, _ot(leg.kind))
    return replace(leg, exit_at=settles, exit_price=value, exit_reason="expiry")


def settle_legs(
    clock: Clock,
    legs: list[SimLeg],
    at: datetime,
    since: datetime | None,
    *,
    multiplier: int = 1,
    rule: Rule | None = None,
    costs: CostModel | None = None,
) -> tuple[list[LegState], Exit | None]:
    """Every leg as of `at`: fills resolved, triggers applied for the span
    stepped over since `since`, marked at the moment's prices, and costed."""
    h = clock.h
    cm = costs or CostModel()
    resolved: list[tuple[SimLeg, int]] = []
    errors: dict[str, LegState] = {}
    for leg in legs:
        try:
            lot = h.lot_size(leg.entry_at.date(), leg.expiry)
        except ValueError:
            errors[leg.id] = LegState(leg, "error", error="lot size unknown for that day")
            continue
        if leg.entry_price is None:
            fill = h.price_asof(leg.key, leg.entry_at)
            if fill is None:
                errors[leg.id] = LegState(leg, "error", lot_size=lot, error="no trade to fill at")
                continue
            leg = replace(leg, entry_price=fill[1])
        if leg.exit_at is not None and leg.exit_price is None:
            fill = h.price_asof(leg.key, leg.exit_at)
            if fill is not None:
                leg = replace(leg, exit_price=fill[1], exit_reason=leg.exit_reason or "exit")
            else:
                leg = replace(leg, exit_at=None, exit_reason=None)
        resolved.append((leg, lot))

    # Moving forward tests what was stepped over. A leg already past its expiry
    # is settled whatever the step, so a jump by date cannot skip it.
    hit: Exit | None = None
    if since is not None and since < at:
        if rule is not None and rule.active:
            walked, hit = _walk(clock, resolved, since, at, rule, multiplier, cm)
            resolved = [(leg, lot) for leg, (_, lot) in zip(walked, resolved, strict=True)]
        else:
            resolved = [
                (_scan(clock, leg, max(since, leg.entry_at), at), lot) for leg, lot in resolved
            ]
    else:
        resolved = [(_settle(clock, leg, at), lot) for leg, lot in resolved]

    by_id = {leg.id: (leg, lot) for leg, lot in resolved}
    out: list[LegState] = []
    for original in legs:
        if original.id in errors:
            out.append(errors[original.id])
            continue
        leg, lot = by_id[original.id]
        if leg.exit_at is not None and leg.exit_at <= at:
            state = LegState(leg, "closed", lot, ltp=leg.exit_price, ltp_at=leg.exit_at)
        else:
            state = _marked(h, LegState(leg, "pending" if at < leg.entry_at else "open", lot), at)
        if state.status != "pending":
            qty = leg.lots * lot * multiplier
            mark = None if state.status == "closed" else state.ltp
            charges, slip = leg_costs(cm, leg, qty, mark, at.date())
            state = replace(state, charges=charges, slippage=slip)
        out.append(state)
    return out, hit


def _marked(h: History, state: LegState, at: datetime) -> LegState:
    """The leg at its last trade as of `at`, with the volatility that price implies."""
    leg = state.leg
    found = h.price_asof(leg.key, at)
    if found is None:
        return state
    ts, price = found
    spot = h.close_at(h.index_symbol, at)
    years = years_to(leg.expiry, _decision(at))
    iv = implied_vol(price, spot, leg.strike, years, _ot(leg.kind)) if spot and years > 0 else None
    return replace(state, ltp=price, ltp_at=ts, iv=iv)


# ----------------------------------------------------------------- payoff


@dataclass(frozen=True)
class Payoff:
    #: P&L now: open legs at their last price, closed ones at their exit.
    pnl: float
    realised: float
    #: (spot, P&L) at the nearest open expiry, and now.
    expiry_curve: list[tuple[float, float]]
    today_curve: list[tuple[float, float]]
    #: None with the matching `unlimited` flag set.
    max_profit: float | None
    max_loss: float | None
    profit_unlimited: bool
    loss_unlimited: bool
    breakevens: list[float]
    #: Chance spot ends the nearest expiry where the expiry curve is above zero,
    #: on a lognormal at the ATM implied volatility. None without one.
    pop: float | None
    #: Spot at -2, -1, +1, +2 standard deviations by the nearest expiry.
    sd: list[float] = field(default_factory=list)
    #: Margin the open legs would need today, by NSE's rules: see optbt/margin.py.
    span: float = 0.0
    exposure: float = 0.0
    #: Charges and slippage of the included legs, an open one's exit included;
    #: `pnl` less these is `net`. The curves and limits are net of them.
    charges: float = 0.0

    @property
    def net(self) -> float:
        return self.pnl - self.charges


#: Either side of spot, the span drawn for a position with nothing left open.
CLOSED_RANGE = 0.05


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def payoff(
    states: list[LegState], at: datetime, spot: float, atm_iv: float | None, multiplier: int
) -> Payoff:
    """What the enabled legs make, now and at the nearest expiry among the open ones."""
    live = [s for s in states if s.leg.enabled and s.status in ("open", "closed")]
    realised = 0.0
    open_: list[LegState] = []
    for s in live:
        qty = s.leg.lots * (s.lot_size or 0) * multiplier
        assert s.leg.entry_price is not None
        if s.status == "closed":
            assert s.leg.exit_price is not None
            realised += s.leg.sign * (s.leg.exit_price - s.leg.entry_price) * qty
        else:
            open_.append(s)
    pnl = realised + sum(
        s.leg.sign * ((s.ltp or s.leg.entry_price or 0) - (s.leg.entry_price or 0))
        * s.leg.lots * (s.lot_size or 0) * multiplier
        for s in open_
    )
    charges = sum(s.charges for s in live)
    # The curves are net: what was booked, less every cost the legs carry.
    booked = realised - charges
    if not open_:
        if not live:
            return Payoff(pnl, realised, [], [], 0.0, 0.0, False, False, [], None)
        # All of it booked: whatever the market does now, the result is the same,
        # so the payoff is flat at what was booked.
        flat = [(spot * (1 - CLOSED_RANGE), booked), (spot * (1 + CLOSED_RANGE), booked)]
        settled = 1.0 if booked > 0 else 0.0
        return Payoff(
            pnl, realised, flat, flat, booked, booked, False, False, [], settled, charges=charges
        )

    margin = estimate(
        [
            MarginLeg(
                option_type=_ot(s.leg.kind),
                strike=s.leg.strike,
                expiry=s.leg.expiry,
                side=s.leg.sign,
                quantity=s.leg.lots * (s.lot_size or 0) * multiplier,
                price=s.ltp if s.ltp is not None else (s.leg.entry_price or 0.0),
                iv=s.iv,
            )
            for s in open_
        ],
        spot,
        at.date(),
        {e: max(years_to(e, _decision(at)), 0.0) for e in {s.leg.expiry for s in open_}},
        atm_iv or 0.15,
    )

    now = _decision(at)
    nearest = min(s.leg.expiry for s in open_)
    t_near = max(years_to(nearest, now), 0.0)
    sigma_ref = atm_iv or 0.15
    sd_move = sigma_ref * math.sqrt(t_near) if t_near > 0 else 0.0
    strikes = [s.leg.strike for s in open_]
    lo = min(spot * math.exp(-4 * sd_move), min(strikes)) * 0.97
    hi = max(spot * math.exp(4 * sd_move), max(strikes)) * 1.03
    n = 241
    grid = [lo + (hi - lo) * i / (n - 1) for i in range(n)]

    def value(s: LegState, x: float, years: float, calibrate: bool) -> float:
        """One leg's worth at spot `x` with `years` left. Calibrated, it passes
        through the traded price at today's spot and the model gives only the
        shape either side of it."""
        ot = _ot(s.leg.kind)
        k = s.leg.strike
        if years <= 0:
            return intrinsic(x, k, ot)
        if s.iv is None:
            # Trading at intrinsic: it moves with the index, at its own premium.
            extra = (s.ltp - intrinsic(spot, k, ot)) if calibrate and s.ltp is not None else 0.0
            return max(0.0, intrinsic(x, k, ot) + extra)
        model = bs.price(x, k, 0.0, s.iv, years, ot)
        if calibrate and s.ltp is not None:
            model += s.ltp - bs.price(spot, k, 0.0, s.iv, years, ot)
        return max(0.0, model)

    def curve(at_expiry: bool) -> list[tuple[float, float]]:
        pts = []
        for x in grid:
            total = booked
            for s in open_:
                if at_expiry:
                    # A later expiry's leg is still worth its time value then.
                    years = (s.leg.expiry - nearest).days / 365 if s.leg.expiry > nearest else 0.0
                else:
                    years = max(years_to(s.leg.expiry, now), 0.0)
                v = value(s, x, years, calibrate=not at_expiry)
                qty = s.leg.lots * (s.lot_size or 0) * multiplier
                total += s.leg.sign * (v - (s.leg.entry_price or 0)) * qty
            pts.append((x, total))
        return pts

    expiry_curve = curve(at_expiry=True)
    today_curve = curve(at_expiry=False)

    single = all(s.leg.expiry == nearest for s in open_)
    if single:
        legs = [
            pay.Leg(
                option_type=_ot(s.leg.kind),
                strike=s.leg.strike,
                premium=s.leg.entry_price or 0.0,
                quantity=s.leg.lots * (s.lot_size or 0) * multiplier,
                side="BUY" if s.leg.side == "buy" else "SELL",
            )
            for s in open_
        ]
        r = pay.analyze(legs, booked)
        max_profit = None if math.isinf(r.max_profit) else r.max_profit
        max_loss = None if math.isinf(r.max_loss) else r.max_loss
        p_unl, l_unl = math.isinf(r.max_profit), math.isinf(r.max_loss)
        breakevens = r.breakevens
    else:
        ys = [y for _, y in expiry_curve]
        max_profit, max_loss = max(ys), min(ys)
        p_unl = l_unl = False
        breakevens = []
        for (x0, y0), (x1, y1) in zip(expiry_curve, expiry_curve[1:], strict=False):
            if (y0 < 0) != (y1 < 0) and y1 != y0:
                breakevens.append(x0 + (x1 - x0) * (-y0) / (y1 - y0))

    pop = None
    sd: list[float] = []
    if sd_move > 0:
        def cdf(x: float) -> float:
            return _norm_cdf((math.log(x / spot) + 0.5 * sd_move**2) / sd_move) if x > 0 else 0.0

        prob = 0.0
        pts = expiry_curve
        prob += cdf(pts[0][0]) * (pts[0][1] > 0)
        prob += (1 - cdf(pts[-1][0])) * (pts[-1][1] > 0)
        for (x0, y0), (x1, y1) in zip(pts, pts[1:], strict=False):
            if (y0 + y1) / 2 > 0:
                prob += cdf(x1) - cdf(x0)
        pop = prob
        sd = [spot * math.exp(k * sd_move) for k in (-2, -1, 1, 2)]

    return Payoff(
        pnl, realised, expiry_curve, today_curve, max_profit, max_loss, p_unl, l_unl,
        breakevens, pop, sd, margin.span, margin.exposure, charges,
    )


# ---------------------------------------------------------------- moment


def _atm(rows: list[ChainRow], spot: float) -> tuple[float | None, float | None]:
    """The strike nearest spot with both options quoted, and their mean IV."""
    priced = [r for r in rows if r.ce and r.pe]
    if not priced:
        return None, None
    row = min(priced, key=lambda r: abs(r.strike - spot))
    ivs = [s.iv for s in (row.ce, row.pe) if s and s.iv]
    return row.strike, (sum(ivs) / len(ivs) if ivs else None)


@dataclass(frozen=True)
class Expiry:
    expiry: date
    days: int
    monthly: bool
    #: "held" (settled, whole), "live" (fetched while trading), "missing".
    status: str = "held"
    #: For a live one, the last bar fetched.
    through: datetime | None = None


@dataclass(frozen=True)
class Moment:
    at: datetime
    first: datetime
    last: datetime
    spot: float
    vix: float | None
    future: tuple[date, float] | None
    expiries: list[Expiry]
    expiry: date | None
    lot_size: int | None
    rows: list[ChainRow]
    forward: float | None
    atm: float | None
    atm_iv: float | None
    legs: list[LegState]
    payoff: Payoff
    #: The whole position squared off by its P&L rule on the way here, if it was.
    squared: Exit | None = None


def moment(
    clock: Clock,
    at: datetime | None,
    move: str | None,
    expiry: date | None,
    legs: list[SimLeg],
    since: datetime | None,
    multiplier: int = 1,
    rule: Rule | None = None,
    costs: CostModel | None = None,
) -> Moment:
    h = clock.h
    at = clock.move(at, move) if (at is not None and move) else clock.snap(at)
    spot = h.close_at(h.index_symbol, at)
    assert spot is not None
    monthlies = set(h.monthly_expiries())
    listed = [e for e in h.expiries() if e >= at.date()][:EXPIRIES_SHOWN]
    status = h.expiry_status(listed)
    expiries = [
        Expiry(e, (e - at.date()).days, e in monthlies, *status.get(e, ("missing", None)))
        for e in listed
    ]
    chosen = expiry if expiry in listed else (listed[0] if listed else None)
    rows: list[ChainRow] = []
    forward = atm = atm_iv = None
    lot = None
    if chosen is not None:
        rows, forward = chain(h, chosen, at, spot)
        atm, atm_iv = _atm(rows, spot)
        try:
            lot = h.lot_size(at.date(), chosen)
        except ValueError:
            lot = None
    states, squared = settle_legs(
        clock, legs, at, since, multiplier=multiplier, rule=rule, costs=costs
    )
    # The payoff's spread of outcomes is read at the nearest open leg's expiry.
    near_iv = atm_iv
    open_exp = [s.leg.expiry for s in states if s.status == "open" and s.leg.enabled]
    if open_exp and min(open_exp) != chosen:
        near_rows, _ = chain(h, min(open_exp), at, spot)
        near_iv = _atm(near_rows, spot)[1]
    vix = h.close_at(INDIA_VIX, at)
    days = clock.days
    first = clock.bars(days[0])[0]
    last = clock.bars(days[-1])[-1]
    return Moment(
        at=at,
        first=first,
        last=last,
        spot=spot,
        vix=vix,
        future=h.future_asof(at),
        expiries=expiries,
        expiry=chosen,
        lot_size=lot,
        rows=rows,
        forward=forward,
        atm=atm,
        atm_iv=atm_iv,
        legs=states,
        payoff=payoff(states, at, spot, near_iv or (vix / 100 if vix else None), multiplier),
        squared=squared,
    )
