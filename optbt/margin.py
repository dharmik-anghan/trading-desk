"""What a position would need as margin, by NSE Clearing's rules as they stand.

Not the margin that was charged on a past day - that came from the exchange's
own risk arrays for that day - but what the same position would be charged
under today's parameters, which is what a "would I have the capital for this"
question needs. It reproduced a broker's figure for a NIFTY short straddle
within 2-8% (Fyers' span_margin, 1 Oct 2026); the rest is the exchange's own
volatility per contract, which is not published as such.

SPAN, from https://www.nseclearing.in/risk-management/equity-derivatives/nsccl-span
and .../span-risk-parameters (updated 20 Jun 2025):

  - Sixteen scenarios over a one-day look-ahead: the underlying unchanged, and
    up and down by 1/3, 2/3 and 3/3 of the price scan range, each with
    volatility up and down by the volatility scan range; and up and down by
    twice the price scan range, of which 35% of the loss is covered.
  - Price scan range for index options: 6 sigma scaled by sqrt(2), at least
    9.3% of the underlying (17.7% beyond nine months).
  - Volatility scan range: 25% of annualised EWMA volatility, at least 4%.
  - The scanning risk is the largest loss of the whole position across them;
    long options are offset by their value (net option value). A long option's
    gain in a scenario offsets the shorts' losses - a spread is margined on
    its width - but a short option's gain does not: Fyers charged a short
    straddle exactly what its put alone needed, 138,201 (1 Oct 2026).

Exposure (extreme loss) margin, from .../risk-management/equity-derivatives/margins:
2% of notional on index derivatives - for a short option, the underlying's
value times the quantity - 3% for one more than 10% out of the money, 5% beyond
nine months, and a further 2% on short index options on their expiry day.

The floors are what apply to NIFTY outside a crisis (6 sigma x sqrt(2) passes
9.3% only once daily volatility passes 1.1%), so they are used as the ranges.
Calendar-spread charges across expiries are not modelled.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from analytics import black_scholes as bs
from broker.models import OptionType

PRICE_SCAN = 0.093
PRICE_SCAN_LONG_DATED = 0.177
VOL_SCAN = 0.04
EXTREME_MULTIPLE = 2.0
EXTREME_COVER = 0.35
LOOK_AHEAD_YEARS = 1 / 365
#: Beyond this many days to expiry, the long-dated scan range and exposure apply.
LONG_DATED_DAYS = 270

EXPOSURE = 0.02
EXPOSURE_DEEP_OTM = 0.03
EXPOSURE_LONG_DATED = 0.05
EXPOSURE_EXPIRY_DAY = 0.02
DEEP_OTM = 0.10

#: Scenarios as (fraction of the price scan range, volatility direction, share of loss covered).
SCENARIOS: tuple[tuple[float, int, float], ...] = (
    *((m, v, 1.0) for m in (0.0, 1 / 3, -1 / 3, 2 / 3, -2 / 3, 1.0, -1.0) for v in (1, -1)),
    (EXTREME_MULTIPLE, 0, EXTREME_COVER),
    (-EXTREME_MULTIPLE, 0, EXTREME_COVER),
)


@dataclass(frozen=True)
class MarginLeg:
    option_type: OptionType
    strike: float
    expiry: date
    #: +1 long, -1 short.
    side: int
    quantity: int
    #: Its price now, and the volatility that price implies (None: none to read).
    price: float
    iv: float | None


@dataclass(frozen=True)
class Margin:
    span: float
    exposure: float

    @property
    def total(self) -> float:
        return self.span + self.exposure


def estimate(
    legs: list[MarginLeg], spot: float, today: date, years: dict[date, float], fallback_iv: float
) -> Margin:
    """SPAN and exposure margin for open legs at `spot`.

    `years` is each expiry's time left, in years; `fallback_iv` prices a leg
    whose own price implies no volatility (deep in the money, all intrinsic).
    """
    if not legs or spot <= 0:
        return Margin(0.0, 0.0)
    long_dated = any((leg.expiry - today).days > LONG_DATED_DAYS for leg in legs)
    scan = spot * (PRICE_SCAN_LONG_DATED if long_dated else PRICE_SCAN)

    worst = 0.0
    for move, vol, cover in SCENARIOS:
        s = spot + move * scan
        loss = 0.0
        for leg in legs:
            t = max(years.get(leg.expiry, 0.0) - LOOK_AHEAD_YEARS, 0.0)
            sigma = max(0.005, (leg.iv or fallback_iv) + vol * VOL_SCAN)
            value = (
                bs.price(s, leg.strike, 0.0, sigma, t, leg.option_type)
                if t > 0
                else max(0.0, (s - leg.strike) if leg.option_type == "CE" else (leg.strike - s))
            )
            # A short loses as the option gains; a long as it loses. A short's
            # gain - the premium it might yet keep - offsets nothing: a broker
            # charged a short straddle exactly its put's margin, not less.
            change = -leg.side * (value - leg.price) * leg.quantity
            loss += max(change, 0.0) if leg.side < 0 else change
        worst = max(worst, loss * cover)

    long_value = sum(leg.price * leg.quantity for leg in legs if leg.side > 0)
    span = max(0.0, worst - long_value)

    exposure = 0.0
    for leg in legs:
        if leg.side > 0:
            continue
        otm = (leg.strike - spot) / spot if leg.option_type == "CE" else (spot - leg.strike) / spot
        if (leg.expiry - today).days > LONG_DATED_DAYS:
            rate = EXPOSURE_LONG_DATED
        elif otm > DEEP_OTM:
            rate = EXPOSURE_DEEP_OTM
        else:
            rate = EXPOSURE
        if leg.expiry <= today:
            rate += EXPOSURE_EXPIRY_DAY
        exposure += rate * spot * leg.quantity
    return Margin(span, exposure)
