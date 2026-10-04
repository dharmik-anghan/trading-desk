"""Margin by NSE Clearing's published rules, against figures worked out from them
and against what a broker charged for the same position."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from optbt.margin import Margin, MarginLeg, estimate
from optbt.marks import implied_vol, years_to

SPOT = 22421.95
TODAY = date(2026, 10, 1)
EXPIRY = date(2026, 10, 6)
YEARS = {EXPIRY: years_to(EXPIRY, datetime(2026, 10, 1, 15, 30))}


def _leg(kind: str, strike: float, price: float, side: int = -1, qty: int = 65) -> MarginLeg:
    iv = implied_vol(price, SPOT, strike, YEARS[EXPIRY], kind)  # type: ignore[arg-type]
    return MarginLeg(kind, strike, EXPIRY, side, qty, price, iv)  # type: ignore[arg-type]


CE = _leg("CE", 22550, 82.95)
PE = _leg("PE", 22550, 181.9)


def _margin(*legs: MarginLeg, today: date = TODAY) -> Margin:
    return estimate(list(legs), SPOT, today, YEARS, 0.12)


def test_exposure_is_two_percent_of_the_index_value_on_each_short() -> None:
    """Fyers charged 29,148.53 a leg on 1 Oct 2026: 2% x 22,421.95 x 65."""
    assert _margin(CE).exposure == pytest.approx(0.02 * SPOT * 65)
    assert _margin(CE, PE).exposure == pytest.approx(2 * 29_148.53, abs=1)


@pytest.mark.parametrize(
    ("legs", "broker"),
    [((CE,), 124_885), ((PE,), 138_201), ((CE, PE), 138_201)],
    ids=["short call", "short put", "short straddle"],
)
def test_span_lands_within_ten_percent_of_what_the_broker_charged(legs, broker) -> None:  # type: ignore[no-untyped-def]
    """Fyers' span_margin for the 06 Oct 22550 contracts on 1 Oct 2026. The
    exchange prices with its own volatility per contract, so not exact."""
    assert _margin(*legs).span == pytest.approx(broker, rel=0.06)


def test_a_short_straddle_needs_what_its_riskier_leg_does() -> None:
    """The call's gain as the market falls does not pay for the put's loss."""
    assert _margin(CE, PE).span == pytest.approx(max(_margin(CE).span, _margin(PE).span))


def test_a_bought_option_needs_no_span_margin_and_no_exposure() -> None:
    m = _margin(_leg("CE", 22550, 82.95, side=1))
    assert (m.span, m.exposure) == (0.0, 0.0)


def test_a_wing_bought_against_a_short_cuts_the_margin() -> None:
    naked = _margin(PE)
    spread = _margin(PE, _leg("PE", 22150, 40.0, side=1))
    # The spread can lose at most its 400-point width less the credit taken.
    assert spread.span < naked.span
    assert spread.span <= (400 - (181.9 - 40.0)) * 65 + 1


def test_far_out_of_the_money_shorts_carry_three_percent_and_expiry_day_two_more() -> None:
    far = _leg("CE", 25000, 1.0)  # 11.5% out of the money
    assert _margin(far).exposure == pytest.approx(0.03 * SPOT * 65)
    assert _margin(CE, today=EXPIRY).exposure == pytest.approx(0.04 * SPOT * 65)
