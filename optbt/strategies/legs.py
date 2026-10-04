"""A strategy built leg by leg.

Every structure is a list of legs: a straddle is two, an iron condor four, a
spread two. Each leg says which way, which option, how many lots, which expiry,
which strike - relative to the money, or by premium - and its own stop and
target. The strategy around them says when to enter, when to leave, on which
weekdays, whether to hold overnight, and what to do with the whole position.

Strike offsets count listed strikes away from the money, signed by moneyness
rather than by price: OTM 2 on a call is two strikes *above* the ATM strike, on
a put two strikes *below*. That is how a structure is described ("sell the OTM 2
call and put") and it means a preset reads the same for either side.

All the legs enter together or none do. A leg whose strike is not quoted, or
whose expiry is not in the store, skips the day with its reason counted -
entering three legs of a four-leg condor would be a different, unhedged trade.

The config is plain data, which is what the optimiser will vary.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import ClassVar, Literal

from analytics import black_scholes as bs
from broker.models import OptionType
from optbt.data.models import Kind
from optbt.engine import Context, Leg, Level, Side, Trade
from optbt.market import OptionKey, Quote, View
from optbt.marks import implied_vol
from optbt.signals import EntrySignal, ExitSignal, Signals
from venues.calendar import NSE_CLOSE


@dataclass(frozen=True)
class ExpiryChoice:
    """Which expiry to trade: the `nth` of a series, passing over any too close.

    Weekly 1st with `min_left` 1 is "the nearest weekly, but not on its expiry
    day - take the next one then". Monthly 2nd is next month.
    """

    #: "daily": every listed expiry. "weekly": the week's own expiries - on the
    #: NSE that is every listed one, so the two mean the same there; a crypto
    #: venue lists dailies between its Friday weeklies, and "weekly" skips them.
    #: "monthly": the month-end ones. "days": the monthly nearest `days`
    #: calendar days out - "45 DTE" - since that is where a trade that long is
    #: placed and where the far strikes have a market.
    series: Literal["daily", "weekly", "monthly", "days"] = "weekly"
    #: 1 is the nearest expiry that qualifies, 2 the one after. Not used by "days".
    nth: int = 1
    #: Expiries with fewer trading sessions than this left are passed over: 1
    #: skips an expiry on its own day, 2 on the day before as well. Sessions, so
    #: the day before a Monday expiry is the Friday.
    min_left: int = 0
    #: For "days": calendar days to expiry to aim for.
    days: int = 45

    #: What the first version of the spec called each choice.
    LEGACY: ClassVar[dict[str, tuple[str, int]]] = {
        "week": ("weekly", 1),
        "next_week": ("weekly", 2),
        "month": ("monthly", 1),
        "next_month": ("monthly", 2),
        "days": ("days", 1),
    }

    @classmethod
    def from_legacy(cls, rule: str, days: int = 45) -> ExpiryChoice:
        """"week", "next_week", "month", "next_month" or "days", as they were."""
        series, nth = cls.LEGACY[rule]
        return cls(series, nth, 0, days)  # type: ignore[arg-type]


@dataclass(frozen=True)
class StrikeRule:
    #: "atm": `offset` listed strikes from the money, + OTM and - ITM.
    #: "premium": the strike whose premium is closest to `premium`.
    #: "pct": the strike nearest `pct` percent from spot, + OTM and - ITM - how a
    #: far strike is described: at 45 days a short sits 4-5% out, forty strikes
    #: away, where counting strikes stops being natural.
    #: "delta": the strike whose delta (absolute) is nearest `delta` - 0.30 to sell,
    #: 0.17 to buy. Worked out from each strike's own premium, since the store
    #: has no greeks: see `strike_deltas`.
    #: "straddle_width": `width_mult` times the ATM straddle's combined premium,
    #: away from the ATM strike - a strangle sized to how much premium is on
    #: the table today, wider when the market is pricing more movement.
    #: "sp_pct": the strike whose own premium is closest to `sp_pct` percent of
    #: the ATM straddle's combined premium - "sell the leg worth a quarter of
    #: the straddle", read off the chain rather than guessed in rupees.
    mode: Literal["atm", "premium", "pct", "delta", "straddle_width", "sp_pct"] = "atm"
    offset: int = 0
    premium: float = 0.0
    pct: float = 0.0
    delta: float = 0.30
    width_mult: float = 1.0
    sp_pct: float = 25.0


@dataclass(frozen=True)
class LegSpec:
    side: Side
    kind: Kind
    lots: int = 1
    #: None trades the strategy's expiry; set, this leg's own - a calendar's far leg.
    expiry: ExpiryChoice | None = None
    strike: StrikeRule = field(default_factory=StrikeRule)
    stop: Level | None = None
    target: Level | None = None


@dataclass(frozen=True)
class DayFilter:
    """Which days to trade at all, judged at the moment of entry.

    Everything is known by then: the pivots and gap from yesterday's close and
    today's open, the VIX at the bar before entry, its rank against earlier days
    only. None means "no condition". Weekdays live on `LegsConfig`.
    """

    #: "only": trade only on the session the first leg expires in; "skip": never
    #: on it; "skip_eve": neither on it nor on the session before.
    expiry_day: Literal["any", "only", "skip", "skip_eve"] = "any"
    #: Calendar days from today to the first leg's expiry.
    dte_min: int | None = None
    dte_max: int | None = None
    vix_min: float | None = None
    vix_max: float | None = None
    #: 0-100, against the previous `vix_lookback` sessions' closes.
    vix_pct_min: float | None = None
    vix_pct_max: float | None = None
    vix_lookback: int = 252
    #: Today's open against yesterday's close, in percent. Signed.
    gap_min: float | None = None
    gap_max: float | None = None
    #: Where the open must sit against today's pivots (optbt.context.ZONES).
    #: {"S1-P", "P-R1"} is "opened between S1 and R1". Empty means anywhere.
    open_zones: frozenset[str] = frozenset()

    def why_not(self, tags: Mapping[str, object]) -> str | None:
        """The first condition these tags fail, or None if the day qualifies."""

        def outside(key: str, low: float | None, high: float | None) -> bool:
            if low is None and high is None:
                return False
            value = tags.get(key)
            if not isinstance(value, int | float):
                return True  # asked for, and not known: not a day that qualifies
            return (low is not None and value < low) or (high is not None and value > high)

        if self.expiry_day == "only" and not tags.get("expiry_day"):
            return "filter: not an expiry day"
        if self.expiry_day == "skip" and tags.get("expiry_day"):
            return "filter: expiry day"
        if self.expiry_day == "skip_eve":
            left = tags.get("sessions_to_expiry")
            if not isinstance(left, int) or left <= 1:
                return "filter: expiry day or the day before"
        if outside("dte", self.dte_min, self.dte_max):
            return "filter: days to expiry"
        if outside("vix", self.vix_min, self.vix_max):
            return "filter: VIX"
        if outside("vix_pct", self.vix_pct_min, self.vix_pct_max):
            return "filter: VIX percentile"
        if outside("gap_pct", self.gap_min, self.gap_max):
            return "filter: gap"
        if self.open_zones and tags.get("open_zone") not in self.open_zones:
            return "filter: open outside the chosen pivot zones"
        return None


@dataclass(frozen=True)
class Adjustment:
    """Moving the untested side in when the market runs at a condor's wing.

    Falls to within `near_points` of the long put: the call spread - in profit
    by now - is closed, and a new one opened with its short `fall_points` above
    the long put (or the short put, by `fall_from`) and, with `move_wing`, its
    long the same width above that. Rises to within `near_points` of the long
    call: the put spread is closed and reopened with its short `rise_points`
    below the short call (or the long call); at 0 below the short call it is an
    iron fly.

    Points, not strikes: "two strikes" meant 200 points to a trader reading a
    100-point chain and 100 to one whose chain lists every 50.

    Moving the wing with the short is what keeps the risk defined. Moving only
    the short left a new short call 22950 hedged by a 24450 wing in Feb 2025 -
    fifteen hundred points of naked exposure - and it lost 33,000 in the March
    rally where the same adjustment with its wing lost a few hundred.

    The position's exits stay measured against the credit it opened for.
    """

    enabled: bool = False
    near_points: float = 50
    fall_from: Literal["long", "short"] = "long"
    fall_points: float = 200
    rise_from: Literal["long", "short"] = "short"
    rise_points: float = 0
    move_wing: bool = True
    #: Rolls allowed in one trade, and never two on the same day.
    max_per_trade: int = 1


@dataclass(frozen=True)
class EntryTrigger:
    """When to try the entry, beyond simply waiting for the clock.

    "time" is the plain case: the first bar at or after `entry`. "move_pct"
    waits for spot to have moved that many percent from its price *at* `entry`
    before taking the trade - a momentum entry rather than a scheduled one.
    "range_breakout" waits for spot to close outside the high-low range formed
    between `entry` and `range_until`, in either direction.

    Either way the day's other limits still apply: nothing fires at or after
    `exit`, and a trigger that never comes is a day not traded, counted like
    any other `ctx.skip`.
    """

    mode: Literal["time", "move_pct", "range_breakout"] = "time"
    move_pct: float = 0.5
    range_until: time | None = None


@dataclass(frozen=True)
class ReEntry:
    """Trying the same legs again after the position goes flat, same day.

    Only for an intraday strategy - a positional one holds past the day the
    question would apply to. `trigger` is what the previous attempt has to
    have ended on: "leg_stop" only after one of its own legs stopped out,
    "mtm_stop" only after the whole-position stop or credit-stop closed it,
    "any" after whatever closed it, a timed exit included - "re-execute" in
    the ordinary sense. `max_times` counts re-entries, not the first entry.
    """

    enabled: bool = False
    trigger: Literal["leg_stop", "mtm_stop", "any"] = "leg_stop"
    max_times: int = 1


@dataclass(frozen=True)
class LegsConfig:
    legs: tuple[LegSpec, ...]
    #: The expiry every leg trades unless it names its own.
    expiry: ExpiryChoice = field(default_factory=ExpiryChoice)
    entry: time = time(9, 20)
    exit: time = time(15, 15)
    #: Monday is 0.
    weekdays: frozenset[int] = frozenset({0, 1, 2, 3, 4})
    #: "intraday": everything is closed at `exit` the same day.
    #: "expiry": held overnight, closed at `exit` on the nearest leg's expiry day
    #: (or settled, if `exit` is after the close).
    hold: Literal["intraday", "expiry"] = "intraday"
    #: Whole-position limits in rupees of combined P&L, checked on each close.
    mtm_stop: float | None = None
    mtm_target: float | None = None
    #: The same, as a fraction of the credit the position took in: 0.5 takes
    #: profit when half the credit is captured, 1.0 stops when the loss equals it.
    #: Ignored for a position opened for a debit, which has no credit to measure.
    target_credit: float | None = None
    stop_credit: float | None = None
    #: Positional only: close at the exit time once the nearest leg is this many
    #: calendar days from expiry - "manage at 21 DTE". None holds to expiry day.
    exit_dte: int | None = None
    #: After any leg stops out, move every other leg's stop to its entry price.
    trail_to_cost: bool = False
    days: DayFilter = field(default_factory=DayFilter)
    adjust: Adjustment = field(default_factory=Adjustment)
    #: After the strikes are chosen, set both wings of a condor to the same
    #: width - the average of the two - so each side risks about the same.
    equal_wings: bool = False
    trigger: EntryTrigger = field(default_factory=EntryTrigger)
    reentry: ReEntry = field(default_factory=ReEntry)
    #: Indicator conditions on the entry (and on each re-entry), and an exit
    #: on them. See optbt.signals.
    entry_signal: EntrySignal = field(default_factory=EntrySignal)
    exit_signal: ExitSignal = field(default_factory=ExitSignal)


def atm_strike(chain: list[Quote], spot: float) -> float | None:
    """Nearest strike to spot at which both a call and a put are priced."""
    calls = {q.key.strike for q in chain if q.key.kind is Kind.CALL and q.price > 0}
    puts = {q.key.strike for q in chain if q.key.kind is Kind.PUT and q.price > 0}
    both = calls & puts
    return min(both, key=lambda k: (abs(k - spot), k)) if both else None


def strike_step(chain: list[Quote], atm: float) -> float | None:
    """The spacing of listed strikes around the money.

    Read from the chain, not assumed: NIFTY lists every 50 near the money and
    every 100 further out, and a far strike's gap is not the one an offset means.
    """
    strikes = sorted({q.key.strike for q in chain})
    near = [k for k in strikes if abs(k - atm) <= atm * 0.03]
    gaps = [b - a for a, b in zip(near, near[1:], strict=False) if b > a]
    if not gaps:
        return None
    return Counter(gaps).most_common(1)[0][0]


#: How far from the asked-for days to expiry a monthly may be and still count.
#: Monthlies are four or five weeks apart, so one is always within about 17.
DAYS_SLACK = 20


def pick_expiry(view: View, choice: ExpiryChoice, *, overnight: bool = False) -> date | None:
    """The expiry a choice means today.

    `overnight` is for a position that will be held past today's close. It
    cannot hold a contract that settles at today's close, so on an expiry day
    "this week" means the next weekly: a positional straddle entered at 15:00 on
    Tuesday 8 Sep 2026 bought the contract expiring that afternoon, and its
    stops were hit within fifteen minutes.
    """
    def enough(e: date) -> bool:
        return choice.min_left == 0 or view.sessions_to(e) >= choice.min_left

    days = choice.days
    if choice.series == "days":
        monthlies = [
            e for e in view.monthly_expiries() if (not overnight or e > view.day) and enough(e)
        ]
        if not monthlies:
            return None
        # Nearest to the target; on a tie, the later one - more time, not less.
        best = min(monthlies, key=lambda e: (abs((e - view.day).days - days), -e.toordinal()))
        # Not one within DAYS_SLACK of the target is no expiry at all. Without
        # this, a run whose calendar ended early traded a "45 DTE" condor on an
        # expiry five days out.
        if abs((best - view.day).days - days) > DAYS_SLACK:
            return None
        return best
    found = {
        "daily": view.expiries,
        "weekly": view.weekly_expiries,
        "monthly": view.monthly_expiries,
    }[choice.series]()
    # Nearest first, and asked about only until the nth has been found: the
    # sessions to an expiry is a question to the calendar.
    seen = 0
    for e in found:
        if (overnight and e <= view.day) or not enough(e):
            continue
        seen += 1
        if seen == choice.nth:
            return e
    return None


#: The expiry's settlement moment, for time to expiry.
EXPIRY_CLOSE = NSE_CLOSE


def strike_deltas(
    chain: list[Quote], spot: float, kind: Kind, now: datetime
) -> dict[float, float]:
    """Each quoted strike's delta, solved from its own premium.

    Black-76 on the forward, with the forward read off the chain by put-call
    parity at the strike nearest the money (call - put + strike). That forward
    already carries the interest and dividends between now and expiry, so no
    rate has to be guessed, and it is what the market itself is pricing to.

    Implied volatility is found by bisection on each option's price; a strike
    whose price is below its intrinsic value, or too small to invert, gets no
    delta rather than a made-up one.
    """
    calls = {q.key.strike: q.price for q in chain if q.key.kind is Kind.CALL and q.price > 0}
    puts = {q.key.strike: q.price for q in chain if q.key.kind is Kind.PUT and q.price > 0}
    both = calls.keys() & puts.keys()
    if not both or not chain:
        return {}
    expiry = chain[0].key.expiry
    years = (datetime.combine(expiry, EXPIRY_CLOSE) - now).total_seconds() / (365 * 24 * 3600)
    if years <= 0:
        return {}
    pivot = min(both, key=lambda k: abs(k - spot))
    forward = calls[pivot] - puts[pivot] + pivot
    option_type: OptionType = "CE" if kind is Kind.CALL else "PE"
    out: dict[float, float] = {}
    for strike, premium in (calls if kind is Kind.CALL else puts).items():
        sigma = implied_vol(premium, forward, strike, years, option_type)
        if sigma is not None:
            out[strike] = bs.greeks(forward, strike, 0.0, sigma, years, option_type).delta
    return out


def atm_straddle_premium(chain: list[Quote], spot: float) -> tuple[float, float] | None:
    """The ATM strike's call + put premium, and the strike itself - or None."""
    atm = atm_strike(chain, spot)
    if atm is None:
        return None
    priced = {(q.key.kind, q.key.strike): q.price for q in chain if q.price > 0}
    call, put = priced.get((Kind.CALL, atm)), priced.get((Kind.PUT, atm))
    if call is None or put is None:
        return None
    return call + put, atm


def pick_strike(
    chain: list[Quote], spot: float, kind: Kind, rule: StrikeRule, now: datetime | None = None
) -> tuple[float | None, str]:
    """The strike a rule means, or None and why not."""
    priced = {q.key.strike: q.price for q in chain if q.key.kind is kind and q.price > 0}
    if rule.mode in ("straddle_width", "sp_pct"):
        found = atm_straddle_premium(chain, spot)
        if found is None:
            return None, "no ATM straddle priced"
        premium, atm = found
        if not priced:
            return None, "no strike priced"
        if rule.mode == "straddle_width":
            direction = 1 if kind is Kind.CALL else -1
            aim = atm + direction * premium * rule.width_mult
            strike = min(priced, key=lambda k: (abs(k - aim), k))
            if abs(strike - aim) > spot * 0.01:
                return None, "strike not quoted"
            return strike, ""
        aim = premium * rule.sp_pct / 100
        return min(priced, key=lambda k: (abs(priced[k] - aim), k)), ""
    if rule.mode == "delta":
        if now is None:
            return None, "no time to measure delta from"
        deltas = strike_deltas(chain, spot, kind, now)
        if not deltas:
            return None, "no delta could be measured"
        strike = min(deltas, key=lambda k: (abs(abs(deltas[k]) - rule.delta), k))
        # Nothing within 0.05 of what was asked is a chain that does not reach it.
        if abs(abs(deltas[strike]) - rule.delta) > 0.05:
            return None, "no strike near that delta"
        return strike, ""
    if rule.mode == "pct":
        if not priced:
            return None, "no strike priced"
        direction = 1 if kind is Kind.CALL else -1
        aim = spot * (1 + direction * rule.pct / 100)
        strike = min(priced, key=lambda k: (abs(k - aim), k))
        # A strike more than 1% of spot from where it was aimed is a gap in the
        # chain, not the strike that was asked for.
        if abs(strike - aim) > spot * 0.01:
            return None, "strike not quoted"
        return strike, ""
    if rule.mode == "premium":
        if not priced:
            return None, "no strike priced"
        return min(priced, key=lambda k: (abs(priced[k] - rule.premium), k)), ""
    atm = atm_strike(chain, spot)
    if atm is None:
        return None, "no strike priced on both sides"
    if rule.offset == 0:
        return atm, ""
    step = strike_step(chain, atm)
    if step is None:
        return None, "no strike spacing near the money"
    direction = 1 if kind is Kind.CALL else -1
    strike = atm + rule.offset * step * direction
    if strike not in priced:
        return None, "strike not quoted"
    return strike, ""


#: How late after the entry time an entry may still happen. A session that opens
#: after it - the 21 Oct 2025 Muhurat session started at 13:45 - is not a "09:20"
#: entry, and trading it as one would be a different strategy.
ENTRY_GRACE = timedelta(minutes=5)


def _plus(t: time, delta: timedelta) -> time:
    return (datetime.combine(date.min, t) + delta).time()


class LegStrategy:
    def __init__(self, config: LegsConfig) -> None:
        self.config = config
        self._tried_today = False
        #: Per trade: how many rolls, and the day of the last one.
        self._rolled: dict[int, tuple[int, date]] = {}
        #: Re-entry bookkeeping for the day: how many have happened, and the id
        #: of the last closed trade already judged (so one close is not read twice).
        self._reentries_today = 0
        self._reacted_closed_id: int | None = None
        #: Trigger bookkeeping: spot at `entry` for "move_pct", the range formed
        #: by `entry` to `range_until` for "range_breakout".
        self._ref_price: float | None = None
        self._range_hi: float | None = None
        self._range_lo: float | None = None
        conditions = config.entry_signal.conditions + config.exit_signal.conditions
        self._signals = Signals(conditions) if conditions else None
        #: Waiting on the entry signal: since the entry time, for the first
        #: entry; since a qualifying close, for a re-entry.
        self._waiting = False
        self._rearmed = False

    def on_day(self, ctx: Context) -> None:
        self._tried_today = False
        self._reentries_today = 0
        self._reacted_closed_id = None
        self._ref_price = None
        self._range_hi = None
        self._range_lo = None
        self._waiting = False
        self._rearmed = False
        if self._signals is not None:
            self._signals.on_day(ctx.view)

    def on_bar(self, ctx: Context) -> None:
        cfg = self.config
        view = ctx.view
        clock = view.clock
        if self._signals is not None:
            self._signals.on_bar(view)
        self._track_trigger(view)

        if ctx.open_legs:
            self._manage(ctx)
            return

        if ctx.pending or view.day.weekday() not in cfg.weekdays:
            return
        if clock >= cfg.exit and (cfg.hold == "intraday" or view.bars_left == 0):
            if self._waiting and not self._tried_today:
                ctx.skip("entry signal never came")
            self._waiting = False
            if cfg.hold == "intraday":
                return

        if self._tried_today:
            self._try_reentry(ctx)
            return
        self._try_first_entry(ctx)

    def _track_trigger(self, view: View) -> None:
        """What `_triggered` will read: the reference price, or the range."""
        trig = self.config.trigger
        clock = view.clock
        if trig.mode == "range_breakout" and trig.range_until is not None:
            if self.config.entry <= clock < trig.range_until:
                spot = view.spot()
                self._range_hi = spot if self._range_hi is None else max(self._range_hi, spot)
                self._range_lo = spot if self._range_lo is None else min(self._range_lo, spot)
        elif trig.mode == "move_pct" and self._ref_price is None and clock >= self.config.entry:
            self._ref_price = view.spot()

    def _triggered(self, view: View) -> bool:
        trig = self.config.trigger
        if trig.mode == "move_pct":
            if not self._ref_price:
                return False
            moved = abs(view.spot() - self._ref_price) / self._ref_price * 100
            return moved >= trig.move_pct
        if trig.mode == "range_breakout":
            if self._range_hi is None or self._range_lo is None:
                return False
            spot = view.spot()
            return spot > self._range_hi or spot < self._range_lo
        return True  # "time": the clock alone is the trigger

    def _entry_start(self) -> time:
        trig = self.config.trigger
        if trig.mode == "range_breakout" and trig.range_until is not None:
            return trig.range_until
        return self.config.entry

    def _try_first_entry(self, ctx: Context) -> None:
        cfg = self.config
        view = ctx.view
        clock = view.clock
        if clock < self._entry_start():
            return
        waits = bool(cfg.entry_signal.conditions) and cfg.entry_signal.mode == "wait"
        if cfg.trigger.mode == "time" and not waits:
            late = _plus(cfg.entry, ENTRY_GRACE)
            if clock >= late:
                self._tried_today = True
                # Counted only when the session itself opened after the entry
                # window (a Muhurat session). A day whose entry time passed
                # while a positional trade was still open is simply not an
                # entry day.
                if view.session_start > cfg.entry:
                    ctx.skip("no session at the entry time")
                return
        if not self._triggered(view):
            return
        verdict, why = self._signal_says()
        if verdict == "wait":
            self._waiting = True
            return
        self._tried_today = True
        self._waiting = False
        if verdict == "skip":
            ctx.skip(why)
            return
        self._enter(ctx)

    def _signal_says(self) -> tuple[Literal["enter", "wait", "skip"], str]:
        """What the entry signal makes of this moment, and why if not "enter"."""
        rule = self.config.entry_signal
        if not rule.conditions or self._signals is None:
            return "enter", ""
        met = self._signals.met(rule.conditions, rule.join)
        if rule.mode == "wait":
            return ("enter", "") if met is True else ("wait", "")
        if met is None:
            return "skip", "entry signal: not enough history"
        if rule.mode == "take_if":
            return ("enter", "") if met else ("skip", "entry signal: not met")
        return ("skip", "entry signal: met") if met else ("enter", "")

    def _try_reentry(self, ctx: Context) -> None:
        re = self.config.reentry
        if not re.enabled or self.config.hold != "intraday" or self._reentries_today >= re.max_times:
            return
        closed = ctx.last_closed
        if closed is not None and closed.id != self._reacted_closed_id:
            self._reacted_closed_id = closed.id
            self._rearmed = self._reentry_qualifies(closed, re.trigger)
        if not self._rearmed:
            return
        verdict, _ = self._signal_says()
        if verdict == "wait":
            return
        self._rearmed = False
        if verdict == "skip":
            return
        self._reentries_today += 1
        self._enter(ctx)

    @staticmethod
    def _reentry_qualifies(trade: Trade, trigger: Literal["leg_stop", "mtm_stop", "any"]) -> bool:
        if trigger == "any":
            return True
        reasons = {leg.exit_reason for leg in trade.legs}
        if trigger == "leg_stop":
            return "stop" in reasons
        return "mtm stop" in reasons  # trigger == "mtm_stop"

    def _expiry_of(self, spec: LegSpec) -> ExpiryChoice:
        return spec.expiry or self.config.expiry

    def _describe(self, view: View, expiry: date) -> dict[str, str | float | int | bool | None]:
        """The day as it stood at this decision - what a trade is tagged with and
        what the day filter judges."""
        day = view.context.day(view.day)
        vix = view.context.vix_at(view.now)
        lookback = self.config.days.vix_lookback
        return {
            "weekday": view.day.strftime("%a"),
            "month": view.day.strftime("%Y-%m"),
            "dte": (expiry - view.day).days,
            "sessions_to_expiry": (sessions := view.sessions_to(expiry)),
            "expiry_day": sessions == 0,
            "monthly_expiry": expiry in view.monthly_expiries(),
            "spot": round(view.spot(), 2),
            "vix": round(vix, 2) if vix is not None else None,
            "vix_pct": (
                round(p, 1)
                if vix is not None
                and (p := view.context.vix_percentile(view.day, vix, lookback)) is not None
                else None
            ),
            "gap_pct": round(day.gap_pct, 2) if day and day.gap_pct is not None else None,
            "open_zone": day.open_zone if day else None,
        }

    def _enter(self, ctx: Context) -> None:
        view = ctx.view
        spot = view.spot()
        overnight = self.config.hold == "expiry"
        first = (
            pick_expiry(view, self._expiry_of(self.config.legs[0]), overnight=overnight)
            if self.config.legs
            else None
        )
        if first is None:
            ctx.skip("no expiry listed")
            return
        tags = self._describe(view, first)
        why = self.config.days.why_not(tags)
        if why is not None:
            ctx.skip(why)
            return
        chains: dict[date, list[Quote]] = {}
        orders: list[tuple[OptionKey, LegSpec]] = []
        for spec in self.config.legs:
            expiry = pick_expiry(view, self._expiry_of(spec), overnight=overnight)
            if expiry is None:
                ctx.skip("no expiry listed")
                return
            if expiry not in chains:
                chains[expiry] = view.chain(expiry)
            chain = chains[expiry]
            if not chain:
                ctx.skip("expiry not in the store")
                return
            strike, why = pick_strike(chain, spot, spec.kind, spec.strike, view.now)
            if strike is None:
                ctx.skip(why)
                return
            orders.append((OptionKey(expiry, strike, spec.kind), spec))
        if self.config.equal_wings:
            orders = _equal_wings(orders, chains)
        ctx.tag(**tags)
        if self._signals is not None:
            for c in self.config.entry_signal.conditions:
                ctx.note(f"entry signal: {self._signals.readout(c)}")
        for i, (key, spec) in enumerate(orders):
            ctx.open(
                key,
                spec.side,
                spec.lots,
                tag=f"leg {i + 1}",
                stop=spec.stop,
                target=spec.target,
            )

    def _manage(self, ctx: Context) -> None:
        cfg = self.config
        view = ctx.view
        legs = ctx.open_legs
        clock = view.clock

        nearest = min(leg.key.expiry for leg in legs)
        if cfg.hold == "intraday":
            last_day = view.day
        elif cfg.exit_dte is not None:
            last_day = min(nearest, nearest - timedelta(days=cfg.exit_dte))
        else:
            last_day = nearest
        if view.day >= last_day and clock >= cfg.exit:
            ctx.close_all("time" if view.day >= nearest or cfg.exit_dte is None else "dte exit")
            return
        if cfg.hold == "intraday" and view.bars_left <= 1:
            # The session ends before the exit time - a Saturday special session
            # closed at 12:29. Out on its last bar, rather than carried over a
            # weekend by a strategy that says intraday.
            ctx.close_all("session end")
            return

        stop, target = cfg.mtm_stop, cfg.mtm_target
        if (cfg.stop_credit is not None or cfg.target_credit is not None) and ctx.trade:
            credit = _credit(ctx.trade.legs)
            if credit > 0:
                if cfg.stop_credit is not None:
                    by_credit = cfg.stop_credit * credit
                    stop = by_credit if stop is None else min(stop, by_credit)
                if cfg.target_credit is not None:
                    by_credit = cfg.target_credit * credit
                    target = by_credit if target is None else min(target, by_credit)
        # Not while its closes are waiting to fill: the decision is already made.
        if (stop is not None or target is not None) and not ctx.pending:
            pnl = ctx.pnl()
            if pnl is not None:
                if stop is not None and pnl <= -stop:
                    ctx.note(f"position P&L {pnl:+,.0f} reached the {stop:,.0f} stop")
                    ctx.close_all("mtm stop")
                    return
                if target is not None and pnl >= target:
                    ctx.note(f"position P&L {pnl:+,.0f} reached the {target:,.0f} target")
                    ctx.close_all("mtm target")
                    return

        rule = cfg.exit_signal
        if rule.conditions and self._signals is not None and ctx.trade and not ctx.pending:
            if self._signals.met(rule.conditions, rule.join, since=ctx.trade.opened):
                for c in rule.conditions:
                    if self._signals.check(c, since=ctx.trade.opened):
                        ctx.note(f"exit signal: {self._signals.readout(c)}")
                ctx.close_all("signal exit")
                return

        if cfg.adjust.enabled and not ctx.pending:
            self._adjust(ctx)

        if cfg.trail_to_cost and ctx.trade is not None:
            if any(leg.exit_reason == "stop" for leg in ctx.trade.legs):
                for leg in legs:
                    against = leg.stop is not None and (leg.stop - leg.entry_price) * -leg.side > 0
                    if against:
                        ctx.set_stop(leg, leg.entry_price)
                        ctx.note(f"stop on {leg.key} moved to cost {leg.entry_price:.2f}")


    def _adjust(self, ctx: Context) -> None:
        """Move the untested side in, if the market has reached a wing."""
        rule = self.config.adjust
        trade = ctx.trade
        view = ctx.view
        if trade is None:
            return
        count, last = self._rolled.get(trade.id, (0, date.min))
        if count >= rule.max_per_trade or last == view.day:
            return

        def one(side: Side, kind: Kind) -> Leg | None:
            found = [leg for leg in ctx.open_legs if leg.side is side and leg.key.kind is kind]
            return found[0] if len(found) == 1 else None

        short_ce, long_ce = one(Side.SELL, Kind.CALL), one(Side.BUY, Kind.CALL)
        short_pe, long_pe = one(Side.SELL, Kind.PUT), one(Side.BUY, Kind.PUT)
        if None in (short_ce, long_ce, short_pe, long_pe):
            return  # not a condor any more: nothing to roll
        assert short_ce and long_ce and short_pe and long_pe
        spot = view.spot()
        if spot <= long_pe.key.strike + rule.near_points:
            anchor = long_pe if rule.fall_from == "long" else short_pe
            aim = anchor.key.strike + rule.fall_points
            short, wing, direction, side_name = short_ce, long_ce, 1, "the long put"
        elif spot >= long_ce.key.strike - rule.near_points:
            anchor = long_ce if rule.rise_from == "long" else short_ce
            aim = anchor.key.strike - rule.rise_points
            short, wing, direction, side_name = short_pe, long_pe, -1, "the long call"
        else:
            return
        chain = view.chain(short.key.expiry)
        kind = short.key.kind
        new_short = _nearest_quoted(chain, kind, aim)
        width = abs(wing.key.strike - short.key.strike)
        new_wing = (
            _nearest_quoted(chain, kind, (new_short or aim) + direction * width)
            if rule.move_wing
            else wing.key.strike
        )
        self._rolled[trade.id] = (count + 1, view.day)
        if new_short is None or new_wing is None:
            ctx.note(f"spot {spot:.0f} near {side_name}, but {aim:g} {kind} is not quoted: no roll")
            return
        if new_short == short.key.strike:
            return
        ctx.note(
            f"spot {spot:.0f} within {rule.near_points:g} of {side_name}: moving the "
            f"{kind} side from {short.key.strike:g}/{wing.key.strike:g} "
            f"to {new_short:g}/{new_wing:g}"
        )
        expiry = short.key.expiry
        ctx.close(short, "adjusted")
        new_short_key = OptionKey(expiry, new_short, kind)
        ctx.open(new_short_key, Side.SELL, short.lots, tag=f"{short.tag} rolled")
        if rule.move_wing and new_wing != wing.key.strike:
            ctx.close(wing, "adjusted")
            new_wing_key = OptionKey(expiry, new_wing, kind)
            ctx.open(new_wing_key, Side.BUY, wing.lots, tag=f"{wing.tag} rolled")


def _nearest_quoted(chain: list[Quote], kind: Kind, aim: float) -> float | None:
    """The quoted strike of this type nearest `aim`, if one is within a strike's reach."""
    strikes = [q.key.strike for q in chain if q.key.kind is kind and q.price > 0]
    if not strikes:
        return None
    best = min(strikes, key=lambda k: (abs(k - aim), k))
    return best if abs(best - aim) <= 100 else None


def _equal_wings(
    orders: list[tuple[OptionKey, LegSpec]], chains: dict[date, list[Quote]]
) -> list[tuple[OptionKey, LegSpec]]:
    """Both wings of a condor set to the same width: the average of the two.

    The 0.17-delta wings of a condor rarely sit the same distance from their
    shorts - skew makes the put side wider - so one side risks more than the
    other. A trader evens them out; this does the same, rounding the width to a
    listed strike. Anything that is not one short and one long per side is left
    as it was.
    """

    def find(side: Side, kind: Kind) -> int | None:
        hits = [i for i, (k, spec) in enumerate(orders) if spec.side is side and k.kind is kind]
        return hits[0] if len(hits) == 1 else None

    sc, lc = find(Side.SELL, Kind.CALL), find(Side.BUY, Kind.CALL)
    sp, lp = find(Side.SELL, Kind.PUT), find(Side.BUY, Kind.PUT)
    if None in (sc, lc, sp, lp):
        return orders
    assert sc is not None and lc is not None and sp is not None and lp is not None
    call_width = orders[lc][0].strike - orders[sc][0].strike
    put_width = orders[sp][0].strike - orders[lp][0].strike
    if call_width <= 0 or put_width <= 0:
        return orders
    width = (call_width + put_width) / 2
    out = list(orders)
    for short_i, long_i, direction in ((sc, lc, 1), (sp, lp, -1)):
        short_key, long_key = orders[short_i][0], orders[long_i][0]
        chain = chains.get(long_key.expiry, [])
        strike = _nearest_quoted(chain, long_key.kind, short_key.strike + direction * width)
        if strike is not None:
            out[long_i] = (OptionKey(long_key.expiry, strike, long_key.kind), orders[long_i][1])
    return out


def _credit(legs: list[Leg]) -> float:
    """What the position took in when it was put on: premium sold minus premium
    bought, in rupees, at the fills of the legs that opened it."""
    if not legs:
        return 0.0
    first = min(leg.entry_ts for leg in legs)
    return sum(-leg.side * leg.entry_price * leg.quantity for leg in legs if leg.entry_ts == first)


# ------------------------------------------------------------------ presets


def _leg(side: Side, kind: Kind, offset: int, stop: Level | None = None) -> LegSpec:
    return LegSpec(side=side, kind=kind, strike=StrikeRule(offset=offset), stop=stop)


#: The stop the short presets carry unless told otherwise.
QUARTER = Level("pct", 0.25)


def straddle(stop: Level | None = QUARTER) -> tuple[LegSpec, ...]:
    return (_leg(Side.SELL, Kind.CALL, 0, stop), _leg(Side.SELL, Kind.PUT, 0, stop))


def iron_condor(short: int = 4, wing: int = 4) -> tuple[LegSpec, ...]:
    return (
        _leg(Side.SELL, Kind.CALL, short),
        _leg(Side.BUY, Kind.CALL, short + wing),
        _leg(Side.SELL, Kind.PUT, short),
        _leg(Side.BUY, Kind.PUT, short + wing),
    )
