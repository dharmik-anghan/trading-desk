"""The simulator over days small enough to work out by hand."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time

import pytest

from optbt.data.models import Kind
from optbt.simulator import Clock, SimLeg, moment
from tests.optbt.test_option_engine import DAY, EXPIRY, LOT, Market

#: Monday 21 Sep and Tuesday 22 Sep 2026, the Tuesday an expiry.
TUE = EXPIRY


def _at(day: date, hh: int, mm: int) -> datetime:
    return datetime.combine(day, time(hh, mm))


def _two_days(ce: dict[time, tuple[float, float, float, float]] | None = None) -> Market:
    m = Market()
    for day in (DAY, TUE):
        m.index(day, close_at=23500.0 if day == TUE else None)
        for strike in (23400.0, 23450.0, 23500.0):
            moves = ce if (strike == 23450 and day == DAY) else None
            m.option(day, strike, Kind.CALL, 100.0, changes=moves)
            m.option(day, strike, Kind.PUT, 100.0)
    return m


def _short_call(**kw: object) -> SimLeg:
    base = SimLeg("a", "sell", Kind.CALL, 23450.0, EXPIRY, 1, _at(DAY, 9, 20))
    return replace(base, **kw)  # type: ignore[arg-type]


def test_moves_carry_across_sessions_bar_by_bar() -> None:
    clock = Clock(_two_days().history())
    at = _at(DAY, 15, 0)
    assert clock.move(at, "+1h") == _at(TUE, 9, 45)
    assert clock.move(at, "eod") == _at(DAY, 15, 29)
    assert clock.move(_at(TUE, 9, 15), "-1m") == _at(DAY, 15, 29)
    assert clock.move(at, "+1d") == _at(TUE, 15, 0)
    assert clock.move(at, "+5d") == _at(TUE, 15, 0)  # the end of the data holds
    assert clock.snap(_at(DAY, 8, 0)) == _at(DAY, 9, 15)


def test_a_leg_fills_at_its_last_price_and_is_marked_at_the_moments() -> None:
    """Sold at 09:20 for 100; the call trades at 130 from 11:00. At 11:30 the
    short is 30 points down on 65: -1,950."""
    m = _two_days(ce={time(11, 0): (130.0, 130.0, 130.0, 130.0)})
    got = moment(Clock(m.history()), _at(DAY, 11, 30), None, None, [_short_call()], None)
    (state,) = got.legs
    assert state.status == "open"
    assert state.leg.entry_price == 100.0
    assert state.ltp == 130.0
    assert state.lot_size == LOT
    assert got.payoff.pnl == pytest.approx(-30 * LOT)


def test_a_step_forward_fills_a_stop_it_went_over() -> None:
    """A 120 stop on the short call: 11:00 trades up to 130 from an open of
    110, so a +1h step from 10:30 closes the leg at 120 on the 11:00 bar."""
    m = _two_days(ce={time(11, 0): (110.0, 130.0, 110.0, 125.0)})
    clock = Clock(m.history())
    leg = _short_call(entry_price=100.0, stop=120.0)
    got = moment(clock, _at(DAY, 10, 30), "+1h", None, [leg], _at(DAY, 10, 30))
    (state,) = got.legs
    assert state.status == "closed"
    assert (state.leg.exit_at, state.leg.exit_price, state.leg.exit_reason) == (
        _at(DAY, 11, 0), 120.0, "stop",
    )
    assert got.payoff.realised == pytest.approx(-20 * LOT)


def test_a_stop_gapped_through_fills_at_the_open() -> None:
    m = _two_days(ce={time(11, 0): (140.0, 145.0, 135.0, 140.0)})
    leg = _short_call(entry_price=100.0, stop=120.0)
    got = moment(Clock(m.history()), _at(DAY, 10, 30), "+1h", None, [leg], _at(DAY, 10, 30))
    assert got.legs[0].leg.exit_price == 140.0


def test_a_jump_without_a_step_does_not_fire_stops() -> None:
    """Opening a moment directly - no `since` - is looking, not living through it."""
    m = _two_days(ce={time(11, 0): (110.0, 130.0, 110.0, 125.0)})
    leg = _short_call(entry_price=100.0, stop=120.0)
    got = moment(Clock(m.history()), _at(DAY, 11, 30), None, None, [leg], None)
    assert got.legs[0].status == "open"


def test_a_leg_still_open_past_its_expiry_settles_at_intrinsic() -> None:
    """The 23450 call, Tuesday's close 23,500: settles at 50, however the
    moment past it was reached."""
    m = _two_days()
    m.index(date(2026, 9, 23))  # a session after the expiry
    clock = Clock(m.history())
    leg = _short_call(entry_price=100.0)
    got = moment(clock, _at(date(2026, 9, 23), 10, 0), None, None, [leg], None)
    (state,) = got.legs
    assert (state.status, state.leg.exit_price, state.leg.exit_reason) == ("closed", 50.0, "expiry")
    assert state.leg.exit_at == _at(TUE, 15, 29)


def test_before_its_entry_a_leg_is_pending_and_out_of_the_payoff() -> None:
    m = _two_days()
    leg = _short_call(entry_at=_at(DAY, 11, 0), entry_price=100.0)
    got = moment(Clock(m.history()), _at(DAY, 10, 0), None, None, [leg], None)
    assert got.legs[0].status == "pending"
    assert got.payoff.pnl == 0.0
    assert got.payoff.expiry_curve == []


def test_a_short_straddles_payoff_is_capped_above_and_unlimited_below() -> None:
    m = _two_days()
    legs = [_short_call(entry_price=100.0), _short_call(id="b", kind=Kind.PUT, entry_price=100.0)]
    p = moment(Clock(m.history()), _at(DAY, 10, 0), None, None, legs, None).payoff
    assert p.max_profit == pytest.approx(200 * LOT)
    assert p.loss_unlimited and p.max_loss is None
    assert p.breakevens == pytest.approx([23250.0, 23650.0])


def test_the_chain_is_as_of_the_moment_with_the_strike_nearest_spot_as_atm() -> None:
    m = _two_days(ce={time(11, 0): (130.0, 130.0, 130.0, 130.0)})
    got = moment(Clock(m.history()), _at(DAY, 10, 59), None, None, [], None)
    assert got.atm == 23450.0
    row = next(r for r in got.rows if r.strike == 23450.0)
    assert row.ce is not None and row.ce.ltp == 100.0  # 11:00 has not closed yet
    assert [e.expiry for e in got.expiries] == [EXPIRY]
