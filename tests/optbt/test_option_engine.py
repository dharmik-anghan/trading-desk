"""The engine against days small enough to work out on paper.

Every expected figure here is computed by hand in the test, from the bars the test
wrote - never by calling the code under test to find out what it does.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta

import duckdb
import pytest

from optbt.costs import Charges, CostModel
from optbt.data.history import History
from optbt.data.models import Candle, Contract, Kind
from optbt.data.store import SCHEMA
from optbt.engine import Context, Engine, Side
from optbt.market import OptionKey
from optbt.strategies.straddle import Straddle, StraddleConfig, atm_strike

DAY = date(2026, 9, 21)
EXPIRY = date(2026, 9, 22)
INDEX = "NSE:NIFTY50-INDEX"
LOT = 65

#: Costs switched off, so the hand arithmetic is premium only. Charges have their
#: own tests below.
FREE = CostModel(brokerage_per_order=0.0, slippage=0.0, min_slip=0.0, schedule=(
    replace(CostModel().schedule[0], stt_sell=0, stt_exercise=0, exchange=0, sebi=0,
            stamp_buy=0, gst=0),
))


def _minutes(day: date) -> list[datetime]:
    start = datetime.combine(day, time(9, 15))
    return [start + timedelta(minutes=i) for i in range(375)]


class Market:
    """Builds a store: a flat index, and contracts whose prices are set per minute."""

    def __init__(self) -> None:
        self.conn = duckdb.connect()
        self.conn.execute(SCHEMA)

    def index(
        self,
        day: date,
        level: float = 23450.0,
        *,
        close_at: float | None = None,
        changes: dict[time, tuple[float, float, float, float]] | None = None,
    ) -> None:
        """A flat index, except for minutes given as (o, h, l, c) - and the very
        last bar of the day, which `close_at` alone can still override."""
        changes = changes or {}
        minutes = _minutes(day)
        last = level
        rows = []
        for ts in minutes:
            o, h, lo, c = changes.get(ts.time(), (last, last, last, last))
            if close_at is not None and ts == minutes[-1]:
                o = h = lo = c = close_at
            last = c
            rows.append(f"('{INDEX}', '1', TIMESTAMP '{ts}', {o}, {h}, {lo}, {c}, 0)")
        self.conn.execute(f"INSERT INTO index_bar VALUES {','.join(rows)}")

    def option(
        self,
        day: date,
        strike: float,
        kind: Kind,
        price: float,
        *,
        changes: dict[time, tuple[float, float, float, float]] | None = None,
        expiry: date = EXPIRY,
    ) -> None:
        """A contract at a flat price, except for minutes given as (o, h, l, c)."""
        changes = changes or {}
        self.conn.execute(
            "INSERT OR IGNORE INTO expiry VALUES ('NIFTY', ?, 'options')", [expiry]
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO contract VALUES (?, 'NIFTY', ?, ?, ?, 1, NULL, NULL, ?)",
            [f"{expiry}{strike}{kind}", expiry, str(kind), strike, datetime(2026, 9, 28)],
        )
        last = price
        rows = []
        for i, ts in enumerate(_minutes(day)):
            o, h, lo, c = changes.get(ts.time(), (last, last, last, last))
            last = c
            # Volume and OI in whole lots that vary, as real ones do - the lot size
            # is read back as their greatest common divisor.
            rows.append(
                f"('NIFTY', DATE '{expiry}', '{kind}', {strike}, TIMESTAMP '{ts}', "
                f"{o}, {h}, {lo}, {c}, {LOT * (i % 7 + 1)}, {LOT * (1000 + i)})"
            )
        self.conn.execute(f"INSERT INTO option_bar VALUES {','.join(rows)}")

    def history(self) -> History:
        return History(self.conn)


def _straddle_day(ce: dict[time, tuple[float, float, float, float]] | None = None,
                  pe: dict[time, tuple[float, float, float, float]] | None = None) -> Market:
    m = Market()
    m.index(DAY)
    for strike in (23400.0, 23450.0, 23500.0):
        m.option(DAY, strike, Kind.CALL, 100.0, changes=ce if strike == 23450 else None)
        m.option(DAY, strike, Kind.PUT, 100.0, changes=pe if strike == 23450 else None)
    return m


def test_a_quiet_day_sells_at_0920_and_buys_back_at_1515() -> None:
    # Both legs sit at 100 until the 15:15 bar opens at 60: each leg makes
    # (100 - 60) x 65 = 2,600, the straddle 5,200.
    at_exit = {time(15, 15): (60.0, 60.0, 60.0, 60.0)}
    m = _straddle_day(ce=at_exit, pe=at_exit)
    result = Engine(m.history(), Straddle(), FREE).run(DAY, DAY)
    (trade,) = result.trades
    assert [(leg.key.strike, leg.entry_ts.time(), leg.exit_ts and leg.exit_ts.time())
            for leg in trade.legs] == [(23450.0, time(9, 20), time(15, 15))] * 2
    assert trade.net == pytest.approx(5_200)
    assert trade.reason == "time"


def test_a_stop_fills_at_its_trigger_when_the_bar_trades_through_it() -> None:
    # CE stop at 125. At 11:00 it opens 110 and trades to 130: bought at 125,
    # a loss of 25 x 65 = 1,625. The PE is bought back at 100 at 15:15.
    m = _straddle_day(ce={time(11, 0): (110.0, 130.0, 108.0, 120.0)})
    (trade,) = Engine(m.history(), Straddle(), FREE).run(DAY, DAY).trades
    ce = next(leg for leg in trade.legs if leg.key.kind is Kind.CALL)
    assert (ce.exit_ts, ce.exit_price, ce.exit_reason) == (
        datetime.combine(DAY, time(11, 0)), 125.0, "stop")
    assert trade.net == pytest.approx(-1_625)


def test_a_stop_gapped_through_fills_at_the_open_not_the_trigger() -> None:
    # The 11:00 bar opens at 150, beyond the 125 stop: there was never a chance
    # to buy at 125. Loss (150 - 100) x 65 = 3,250 on the call.
    m = _straddle_day(ce={time(11, 0): (150.0, 155.0, 140.0, 145.0)})
    (trade,) = Engine(m.history(), Straddle(), FREE).run(DAY, DAY).trades
    ce = next(leg for leg in trade.legs if leg.key.kind is Kind.CALL)
    assert ce.exit_price == 150.0
    assert trade.net == pytest.approx(-3_250)


def test_trailing_to_cost_buys_the_other_leg_back_at_its_entry() -> None:
    # The PE falls to 90 at 10:30; the CE stops at 11:00, and the PE stop moves to
    # 100. At 12:00 the PE trades to 101, so it is bought back at 100: -1,625 on
    # the call, 0 on the put.
    m = _straddle_day(
        ce={time(11, 0): (110.0, 130.0, 108.0, 120.0)},
        pe={time(10, 30): (90.0, 90.0, 90.0, 90.0), time(12, 0): (95.0, 101.0, 95.0, 98.0)},
    )
    strategy = Straddle(StraddleConfig(trail_to_cost=True))
    (trade,) = Engine(m.history(), strategy, FREE).run(DAY, DAY).trades
    pe = next(leg for leg in trade.legs if leg.key.kind is Kind.PUT)
    assert (pe.exit_ts and pe.exit_ts.time(), pe.exit_price) == (time(12, 0), 100.0)
    assert trade.net == pytest.approx(-1_625)


def test_the_decision_uses_the_close_before_and_the_fill_the_open_after() -> None:
    # The 09:20 bar opens at 90 and closes at 200. The sale is at 90: the open of
    # the bar after the 09:19 close the decision was made on.
    m = _straddle_day(ce={time(9, 20): (90.0, 200.0, 90.0, 200.0),
                          time(9, 21): (100.0, 100.0, 100.0, 100.0)})
    strategy = Straddle(StraddleConfig(stop_pct=5.0))
    (trade,) = Engine(m.history(), strategy, FREE).run(DAY, DAY).trades
    ce = next(leg for leg in trade.legs if leg.key.kind is Kind.CALL)
    assert ce.entry_price == 90.0


def test_nothing_after_a_moment_changes_what_happened_before_it() -> None:
    # The same day twice, differing only after 12:00. Every event up to 12:00
    # must be identical: if one is not, something read a price from the future.
    def run(wild: bool) -> list[str]:
        after = {time(h, mi): (999.0, 999.0, 1.0, 999.0)
                 for h in range(12, 16) for mi in range(60) if (h, mi) <= (15, 29)} if wild else {}
        m = Market()
        m.index(DAY)
        for strike in (23400.0, 23450.0, 23500.0):
            for kind in (Kind.CALL, Kind.PUT):
                moves = {time(10, 0): (100.0, 118.0, 95.0, 110.0), **after}
                m.option(DAY, strike, kind, 100.0, changes=moves)
        (trade,) = Engine(m.history(), Straddle(), FREE).run(DAY, DAY).trades
        return [e for e in trade.events if e[11:16] < "12:00"]

    calm, wild = run(False), run(True)
    assert calm == wild
    assert calm  # and there was something to compare


def test_an_open_short_settles_at_intrinsic_on_expiry_day() -> None:
    # Sold the day before, held to expiry; the index settles at 23500. The call
    # is worth 50, the put nothing: (100 - 50) x 65 + 100 x 65 = 9,750.
    from optbt.strategies.legs import LegsConfig, LegSpec, LegStrategy

    m = Market()
    m.index(DAY)
    m.index(EXPIRY, close_at=23500.0)
    for day in (DAY, EXPIRY):
        for strike in (23400.0, 23450.0, 23500.0):
            m.option(day, strike, Kind.CALL, 100.0)
            m.option(day, strike, Kind.PUT, 100.0)
    legs = (LegSpec(Side.SELL, Kind.CALL), LegSpec(Side.SELL, Kind.PUT))
    # An exit time after the close means it is never bought back: it settles.
    strategy = LegStrategy(LegsConfig(legs=legs, hold="expiry", exit=time(23, 0)))
    (trade,) = Engine(m.history(), strategy, FREE).run(DAY, EXPIRY).trades
    assert sorted((leg.key.kind, leg.exit_price) for leg in trade.legs) == [
        (Kind.CALL, 50.0), (Kind.PUT, 0.0)]
    assert trade.reason == "expiry"
    assert trade.net == pytest.approx(9_750)


class _LongCall:
    """Buys one ATM call at 09:20 and holds it."""

    def on_day(self, ctx: Context) -> None:
        pass

    def on_bar(self, ctx: Context) -> None:
        if ctx.view.clock == time(9, 20):
            ctx.open(OptionKey(EXPIRY, 23450.0, Kind.CALL), Side.BUY, 1, "long")


def test_a_long_option_exercised_in_the_money_pays_stt_on_intrinsic() -> None:
    m = Market()
    m.index(EXPIRY, close_at=23550.0)
    m.option(EXPIRY, 23450.0, Kind.CALL, 80.0)
    costs = CostModel(brokerage_per_order=0.0, slippage=0.0, min_slip=0.0)
    (trade,) = Engine(m.history(), _LongCall(), costs).run(EXPIRY, EXPIRY).trades
    (leg,) = trade.legs
    # Intrinsic 100 x 65 = 6,500 of settlement value; STT at 0.15% (post Apr 2026).
    exercise_stt = 6_500 * 0.0015
    buy_stt = 0.0
    assert leg.exit_price == 100.0
    assert leg.charges.stt == pytest.approx(buy_stt + exercise_stt)


# --------------------------------------------------------------- the pieces


def test_atm_is_the_nearest_strike_quoted_on_both_sides() -> None:
    m = Market()
    m.index(DAY)
    m.option(DAY, 23450.0, Kind.CALL, 100.0)  # no put at 23450
    m.option(DAY, 23500.0, Kind.CALL, 80.0)
    m.option(DAY, 23500.0, Kind.PUT, 120.0)
    chain = m.history().chain_at(EXPIRY, datetime.combine(DAY, time(9, 19)))
    assert atm_strike(chain, 23451.0) == 23500.0


def test_the_lot_size_is_read_from_open_interest() -> None:
    m = _straddle_day()
    assert m.history().lot_size(DAY, EXPIRY) == 65


def test_an_impossible_lot_size_is_refused_rather_than_used() -> None:
    m = Market()
    m.index(DAY)
    m.conn.execute(
        "INSERT INTO option_bar VALUES ('NIFTY', ?, 'CE', 23450, ?, 1, 1, 1, 1, 7, 7)",
        [EXPIRY, datetime.combine(DAY, time(9, 15))],
    )
    with pytest.raises(ValueError, match="lot size"):
        m.history().lot_size(DAY, EXPIRY)


def test_a_stray_figure_does_not_change_the_lot() -> None:
    # One bar's open interest that is not a whole lot, among hundreds that are -
    # the shape of the volume figures seen on 3 Jul 2026.
    m = _straddle_day()
    m.conn.execute(
        "UPDATE option_bar SET oi = 45502 WHERE ts = ? AND kind = 'CE' AND strike = 23450",
        [datetime.combine(DAY, time(9, 15))],
    )
    assert m.history().lot_size(DAY, EXPIRY) == 65


def test_charges_on_a_sale_are_on_premium_at_the_rates_of_the_day() -> None:
    costs = CostModel()
    # 100 premium x 65 = 6,500 of turnover, sold, in September 2026.
    c = costs.fill(date(2026, 9, 21), 100.0, 65, buy=False)
    assert c.stt == pytest.approx(6_500 * 0.0015)
    assert c.exchange == pytest.approx(6_500 * 0.0003553)
    assert c.stamp == 0.0
    assert c.gst == pytest.approx((20 + c.exchange + c.sebi) * 0.18)
    # The same sale in 2023 paid the older, lower STT.
    assert costs.fill(date(2023, 6, 1), 100.0, 65, buy=False).stt == pytest.approx(6_500 * 0.000625)


def test_a_purchase_pays_stamp_duty_and_no_stt() -> None:
    c = CostModel().fill(date(2026, 9, 21), 100.0, 65, buy=True)
    assert c.stt == 0.0
    assert c.stamp == pytest.approx(6_500 * 0.00003)


def test_slippage_is_a_share_of_premium_with_a_tick_floor() -> None:
    costs = CostModel()
    assert costs.slip(100.0) == pytest.approx(0.3)
    assert costs.slip(2.0) == 0.05


def test_charges_add() -> None:
    assert (Charges(brokerage=20, stt=1) + Charges(brokerage=20, gst=2)).total == 43


def test_contract_fixture_matches_the_store_schema() -> None:
    # Guards the fixture itself: a Contract written by the real store reads back.
    from optbt.data.store import OptionStore

    store = OptionStore()
    store.write_contract(
        Contract("NSE:X", "NIFTY", EXPIRY, Kind.CALL, 23450.0),
        [Candle(datetime.combine(DAY, time(9, 15)), 1, 1, 1, 1, 65, 650)],
        fetched_at=datetime(2026, 9, 28),
    )
    assert store.held(["NSE:X"]) == {"NSE:X"}


def test_a_day_whose_expiry_was_never_fetched_is_skipped_not_traded_elsewhere() -> None:
    # The calendar lists the 22nd, but only the 29th's contracts are stored. The
    # straddle must not quietly trade the 29th instead.
    m = Market()
    m.index(DAY)
    m.conn.execute("INSERT INTO expiry VALUES ('NIFTY', ?, 'options')", [EXPIRY])
    later = date(2026, 9, 29)
    for kind in (Kind.CALL, Kind.PUT):
        m.option(DAY, 23450.0, kind, 100.0, expiry=later)
    result = Engine(m.history(), Straddle(), FREE).run(DAY, DAY)
    assert result.trades == []
    assert result.skipped == {"expiry not in the store": 1}


# ------------------------------------------------------------ the leg builder

from optbt.engine import Level  # noqa: E402
from optbt.strategies.legs import (  # noqa: E402
    ExpiryChoice,
    LegsConfig,
    LegSpec,
    LegStrategy,
    StrikeRule,
    iron_condor,
    strike_step,
)

STRIKES = [23250.0 + 50 * i for i in range(9)]  # 23250 .. 23650, ATM 23450


def _chain_day(*days: date,
               changes: dict[tuple[float, Kind], dict[time, tuple[float, float, float, float]]]
               | None = None, expiry: date = EXPIRY) -> Market:
    """Nine strikes 50 apart around 23450; calls cheaper upward, puts downward."""
    m = Market()
    for day in days or (DAY,):
        m.index(day)
        for k in STRIKES:
            for kind in (Kind.CALL, Kind.PUT):
                otm = (k - 23450) / 50 * (1 if kind is Kind.CALL else -1)
                price = max(5.0, 100.0 - 15 * otm)
                moves = (changes or {}).get((k, kind))
                m.option(day, k, kind, price, changes=moves, expiry=expiry)
    return m


def _run(m: Market, config: LegsConfig, start: date = DAY, end: date = DAY):  # type: ignore[no-untyped-def]
    return Engine(m.history(), LegStrategy(config), FREE).run(start, end)


def test_otm_offsets_go_up_for_calls_and_down_for_puts() -> None:
    m = _chain_day()
    (trade,) = _run(m, LegsConfig(legs=iron_condor(short=2, wing=2))).trades
    got = sorted((leg.key.kind, leg.side, leg.key.strike) for leg in trade.legs)
    assert got == sorted([
        (Kind.CALL, Side.SELL, 23550.0), (Kind.CALL, Side.BUY, 23650.0),
        (Kind.PUT, Side.SELL, 23350.0), (Kind.PUT, Side.BUY, 23250.0),
    ])


def test_itm_is_a_negative_offset() -> None:
    m = _chain_day()
    leg = LegSpec(Side.BUY, Kind.CALL, strike=StrikeRule(offset=-1))
    (trade,) = _run(m, LegsConfig(legs=(leg,))).trades
    assert trade.legs[0].key.strike == 23400.0


def test_premium_mode_takes_the_strike_nearest_the_premium() -> None:
    # Calls: 23450 -> 100, 23500 -> 85, 23550 -> 70. Nearest to 72 is 23550.
    m = _chain_day()
    leg = LegSpec(Side.SELL, Kind.CALL, strike=StrikeRule(mode="premium", premium=72))
    (trade,) = _run(m, LegsConfig(legs=(leg,))).trades
    assert trade.legs[0].key.strike == 23550.0


def test_all_legs_enter_or_none_do() -> None:
    # OTM 20 does not exist. The condor's other three legs must not be sold alone.
    m = _chain_day()
    far_wing = LegSpec(Side.BUY, Kind.PUT, strike=StrikeRule(offset=20))
    legs = (*iron_condor(short=2, wing=2)[:3], far_wing)
    result = _run(m, LegsConfig(legs=legs))
    assert result.trades == []
    assert result.skipped == {"strike not quoted": 1}


def test_strike_spacing_is_read_near_the_money_not_from_the_wings() -> None:
    m = _chain_day()
    m.option(DAY, 25000.0, Kind.CALL, 1.0)  # a far strike with a large gap
    chain = m.history().chain_at(EXPIRY, datetime.combine(DAY, time(9, 19)))
    assert strike_step(chain, 23450.0) == 50.0


def test_a_target_fills_only_when_traded_through() -> None:
    # Sold at 100 with a 20-point target at 80. 11:00 touches 80 exactly - no fill.
    # 12:00 trades to 79 - filled at 80. (100 - 80) x 65 = 1,300.
    changes = {(23450.0, Kind.CALL): {
        time(11, 0): (85.0, 85.0, 80.0, 82.0),
        time(12, 0): (82.0, 82.0, 79.0, 79.5),
    }}
    m = _chain_day(changes=changes)
    leg = LegSpec(Side.SELL, Kind.CALL, target=Level("points", 20))
    (trade,) = _run(m, LegsConfig(legs=(leg,))).trades
    (sold,) = trade.legs
    assert (sold.exit_ts, sold.exit_price, sold.exit_reason) == (
        datetime.combine(DAY, time(12, 0)), 80.0, "target")
    assert trade.net == pytest.approx(1_300)


def test_a_bar_reaching_both_stop_and_target_is_taken_as_the_stop() -> None:
    changes = {(23450.0, Kind.CALL): {time(11, 0): (100.0, 130.0, 70.0, 100.0)}}
    m = _chain_day(changes=changes)
    leg = LegSpec(Side.SELL, Kind.CALL, stop=Level("pct", 0.25), target=Level("pct", 0.25))
    (trade,) = _run(m, LegsConfig(legs=(leg,))).trades
    assert trade.legs[0].exit_reason == "stop"
    assert trade.legs[0].exit_price == 125.0


def test_a_position_stop_closes_every_leg_on_the_next_open() -> None:
    # Short straddle, no leg stops, whole-position stop at 1,000. At 11:00 the call
    # closes at 120: -20 x 65 = -1,300 marked, beyond the stop. Both legs are bought
    # back at the 11:01 open (still 120 and 100): -1,300 net.
    changes = {(23450.0, Kind.CALL): {time(11, 0): (100.0, 120.0, 100.0, 120.0)}}
    m = _chain_day(changes=changes)
    legs = tuple(LegSpec(Side.SELL, k) for k in (Kind.CALL, Kind.PUT))
    (trade,) = _run(m, LegsConfig(legs=legs, mtm_stop=1_000)).trades
    assert {leg.exit_reason for leg in trade.legs} == {"mtm stop"}
    assert {leg.exit_ts for leg in trade.legs} == {datetime.combine(DAY, time(11, 1))}
    assert trade.net == pytest.approx(-1_300)


def test_a_positional_trade_is_held_overnight_and_closed_on_expiry_day() -> None:
    m = _chain_day(DAY, EXPIRY)  # the 21st, and the 22nd - expiry day
    legs = (LegSpec(Side.SELL, Kind.CALL),)
    cfg = LegsConfig(legs=legs, hold="expiry", exit=time(15, 0))
    (trade,) = _run(m, cfg, DAY, EXPIRY).trades
    (leg,) = trade.legs
    assert leg.entry_ts.date() == DAY
    assert leg.exit_ts == datetime.combine(EXPIRY, time(15, 0))
    assert leg.exit_reason == "time"


def test_weekdays_not_chosen_are_not_traded() -> None:
    m = _chain_day()  # the 21st is a Monday
    legs = (LegSpec(Side.SELL, Kind.CALL),)
    assert _run(m, LegsConfig(legs=legs, weekdays=frozenset({1, 2, 3, 4}))).trades == []


def test_monthly_expiry_is_the_one_futures_expire_on() -> None:
    monthly = date(2026, 9, 29)
    m = _chain_day(expiry=monthly)
    m.conn.execute("INSERT INTO expiry VALUES ('NIFTY', ?, 'futures')", [monthly])
    m.conn.execute("INSERT INTO expiry VALUES ('NIFTY', ?, 'options')", [EXPIRY])
    leg = LegSpec(Side.SELL, Kind.CALL, expiry=ExpiryChoice("monthly"))
    (trade,) = _run(m, LegsConfig(legs=(leg,))).trades
    assert trade.legs[0].key.expiry == monthly



def test_an_intraday_trade_is_closed_when_the_session_ends_early() -> None:
    # A Saturday special session that ended at 12:29 (2 Mar 2024): the 15:15
    # exit never comes, and an intraday trade must not be carried to Monday.
    m = Market()
    start = datetime.combine(DAY, time(9, 15))
    short_day = [start + timedelta(minutes=i) for i in range(195)]  # to 12:29
    m.conn.execute("INSERT INTO index_bar VALUES " + ",".join(
        f"('{INDEX}', '1', TIMESTAMP '{ts}', 23450, 23450, 23450, 23450, 0)" for ts in short_day))
    for kind in (Kind.CALL, Kind.PUT):
        m.option(DAY, 23450.0, kind, 100.0)
    (trade,) = Engine(m.history(), Straddle(), FREE).run(DAY, DAY).trades
    assert {leg.exit_reason for leg in trade.legs} == {"session end"}
    assert {leg.exit_ts for leg in trade.legs} == {datetime.combine(DAY, time(12, 29))}


def test_a_session_that_opens_after_the_entry_time_is_not_traded() -> None:
    # The 21 Oct 2025 Muhurat session opened at 13:45. A 09:20 strategy does not
    # enter at 13:46.
    m = Market()
    start = datetime.combine(DAY, time(13, 45))
    late_day = [start + timedelta(minutes=i) for i in range(61)]
    m.conn.execute("INSERT INTO index_bar VALUES " + ",".join(
        f"('{INDEX}', '1', TIMESTAMP '{ts}', 23450, 23450, 23450, 23450, 0)" for ts in late_day))
    for kind in (Kind.CALL, Kind.PUT):
        m.option(DAY, 23450.0, kind, 100.0)
    result = Engine(m.history(), Straddle(), FREE).run(DAY, DAY)
    assert result.trades == []
    assert result.skipped == {"no session at the entry time": 1}


def test_a_leg_whose_expiry_is_a_holiday_settles_on_the_session_before() -> None:
    # The calendar lists 29 Jun 2023, a holiday; the contracts expired on the 28th.
    holiday = date(2026, 9, 22)  # stands in for the listed-but-closed date
    m = Market()
    m.index(DAY, close_at=23500.0)  # the 21st is the last session before it
    m.index(date(2026, 9, 23))
    for kind in (Kind.CALL, Kind.PUT):
        m.option(DAY, 23450.0, kind, 100.0, expiry=holiday)
    from optbt.strategies.legs import LegsConfig, LegSpec, LegStrategy

    legs = (LegSpec(Side.SELL, Kind.CALL),)
    strategy = LegStrategy(LegsConfig(legs=legs, hold="expiry", exit=time(23, 0)))
    (trade,) = Engine(m.history(), strategy, FREE).run(DAY, date(2026, 9, 23)).trades
    assert trade.legs[0].exit_reason == "expiry"
    assert trade.legs[0].exit_ts is not None and trade.legs[0].exit_ts.date() == DAY



def test_old_lot_positions_in_open_interest_do_not_shrink_the_lot() -> None:
    # The 27 Mar 2025 monthly: 75 was the lot, but 6% of its open interest was
    # still lots of 25 from 2024. 25 divides every figure; it must not win.
    from optbt.data.history import _vote_lot

    oi = [75 * k for k in range(1, 95)] + [25 * k for k in range(1, 20) if k % 3]
    volume = [75 * k for k in range(1, 50)]
    assert _vote_lot((oi, volume), frozenset({25, 50, 65, 75})) == 75


def test_stray_volumes_do_not_hide_the_lot_open_interest_shows() -> None:
    # 25 Mar 2026: lot 65, open interest 89% whole lots, volume only 60%.
    from optbt.data.history import _vote_lot

    oi = [65 * k for k in range(1, 90)] + [75 * k for k in range(1, 11)]
    volume = [65 * k for k in range(1, 61)] + [45502 + k for k in range(40)]
    assert _vote_lot((oi, volume), frozenset({25, 50, 65, 75})) == 65


def test_a_trade_carries_its_days_context_and_its_worst_and_best() -> None:
    # The quiet day: tagged with its weekday and days to expiry, and a call that
    # traded to 118 at 10:00 marks the worst point at -18 x 65 on that leg.
    m = _straddle_day(ce={time(10, 0): (100.0, 118.0, 100.0, 118.0),
                          time(10, 1): (100.0, 100.0, 100.0, 100.0)})
    strategy = Straddle(StraddleConfig(stop_pct=5.0))
    (trade,) = Engine(m.history(), strategy, FREE).run(DAY, DAY).trades
    assert trade.tags["weekday"] == "Mon" and trade.tags["dte"] == 1
    assert trade.tags["expiry_day"] is False
    assert trade.worst == pytest.approx(-18 * 65)


def test_a_day_filtered_out_is_counted_with_its_reason() -> None:
    from optbt.strategies.legs import DayFilter, LegsConfig, LegSpec, LegStrategy

    m = _straddle_day()
    legs = (LegSpec(Side.SELL, Kind.CALL), LegSpec(Side.SELL, Kind.PUT))
    cfg = LegsConfig(legs=legs, days=DayFilter(expiry_day="only"))
    result = Engine(m.history(), LegStrategy(cfg), FREE).run(DAY, DAY)
    assert result.trades == [] and result.skipped == {"filter: not an expiry day": 1}


def test_a_positional_entry_on_expiry_day_takes_the_next_expiry() -> None:
    # 8 Sep 2026: a positional straddle entered at 15:00 on expiry day bought the
    # contract settling that afternoon. Held overnight, it must take the next one.
    from optbt.strategies.legs import LegsConfig, LegSpec, LegStrategy

    next_week = date(2026, 9, 29)
    m = Market()
    m.index(EXPIRY)
    for expiry in (EXPIRY, next_week):
        for kind in (Kind.CALL, Kind.PUT):
            m.option(EXPIRY, 23450.0, kind, 100.0, expiry=expiry)
    legs = (LegSpec(Side.SELL, Kind.CALL), LegSpec(Side.SELL, Kind.PUT))
    positional = LegsConfig(legs=legs, hold="expiry", entry=time(15, 0), exit=time(23, 0))
    (trade,) = Engine(m.history(), LegStrategy(positional), FREE).run(EXPIRY, EXPIRY).trades
    assert {leg.key.expiry for leg in trade.legs} == {next_week}
    # Intraday on the same day still trades the contract expiring today.
    intraday = LegsConfig(legs=legs, entry=time(9, 20), exit=time(15, 15))
    (same_day,) = Engine(m.history(), LegStrategy(intraday), FREE).run(EXPIRY, EXPIRY).trades
    assert {leg.key.expiry for leg in same_day.legs} == {EXPIRY}


# ------------------------------------------------------ 45 DTE and credit exits


def test_expiry_by_days_takes_the_monthly_nearest_to_the_target() -> None:
    # From 21 Sep: 27 Oct is 36 days, 24 Nov is 64. Nearest to 45 is 27 Oct.
    from optbt.strategies.legs import ExpiryChoice, LegsConfig, LegSpec, LegStrategy

    oct_, nov = date(2026, 10, 27), date(2026, 11, 24)
    m = Market()
    m.index(DAY)
    for e in (oct_, nov):
        m.conn.execute("INSERT INTO expiry VALUES ('NIFTY', ?, 'futures')", [e])
        for kind in (Kind.CALL, Kind.PUT):
            m.option(DAY, 23450.0, kind, 100.0, expiry=e)
    leg = LegSpec(Side.SELL, Kind.CALL, expiry=ExpiryChoice("days", days=45))
    cfg = LegsConfig(legs=(leg,), hold="expiry", exit=time(23, 0))
    (trade,) = Engine(m.history(), LegStrategy(cfg), FREE).run(DAY, DAY).trades
    assert trade.legs[0].key.expiry == oct_


def test_a_strike_by_percent_from_spot() -> None:
    # Spot 23450. 0.5% OTM for a call aims at 23567 - nearest listed is 23550; for
    # a put 23333 - nearest is 23350.
    from optbt.strategies.legs import LegsConfig, LegSpec, LegStrategy, StrikeRule

    m = _chain_day()
    legs = (LegSpec(Side.SELL, Kind.CALL, strike=StrikeRule(mode="pct", pct=0.5)),
            LegSpec(Side.SELL, Kind.PUT, strike=StrikeRule(mode="pct", pct=0.5)))
    (trade,) = Engine(m.history(), LegStrategy(LegsConfig(legs=legs)), FREE).run(DAY, DAY).trades
    assert sorted(leg.key.strike for leg in trade.legs) == [23350.0, 23550.0]


def _condor_market(short_at_1100: float) -> Market:
    """Shorts sold at 100, wings bought at 20: a credit of 160 x 65 = 10,400."""
    m = Market()
    m.index(DAY)
    moves = {time(11, 0): (100.0, 100.0, short_at_1100, short_at_1100),
             time(11, 1): (short_at_1100,) * 4}
    for strike, price, change in ((23450.0, 100.0, moves), (23650.0, 20.0, None)):
        for kind in (Kind.CALL, Kind.PUT):
            m.option(DAY, strike if kind is Kind.CALL else 46900.0 - strike, kind, price,
                     changes=change)
    return m


def _condor(target: float | None, stop: float | None):  # type: ignore[no-untyped-def]
    from optbt.strategies.legs import LegsConfig, LegSpec, LegStrategy, StrikeRule

    # The fixture lists strikes 200 apart, so the wings are one strike out.
    legs = (LegSpec(Side.SELL, Kind.CALL), LegSpec(Side.SELL, Kind.PUT),
            LegSpec(Side.BUY, Kind.CALL, strike=StrikeRule(offset=1)),
            LegSpec(Side.BUY, Kind.PUT, strike=StrikeRule(offset=1)))
    return LegStrategy(LegsConfig(legs=legs, target_credit=target, stop_credit=stop))


def test_take_profit_at_half_the_credit() -> None:
    # Shorts fall from 100 to 60: 40 x 65 x 2 = 5,200 - exactly half the 10,400.
    m = _condor_market(60.0)
    (trade,) = Engine(m.history(), _condor(0.5, 1.0), FREE).run(DAY, DAY).trades
    assert {leg.exit_reason for leg in trade.legs} == {"mtm target"}
    assert {leg.exit_ts for leg in trade.legs} == {datetime.combine(DAY, time(11, 1))}


def test_stop_when_the_loss_equals_the_credit() -> None:
    # Shorts rise from 100 to 180: -80 x 65 x 2 = -10,400, the whole credit.
    m = _condor_market(180.0)
    (trade,) = Engine(m.history(), _condor(0.5, 1.0), FREE).run(DAY, DAY).trades
    assert {leg.exit_reason for leg in trade.legs} == {"mtm stop"}
    assert trade.net == pytest.approx(-10_400)


def test_short_of_either_level_it_runs_to_the_exit_time() -> None:
    m = _condor_market(70.0)  # +3,900: less than half the credit
    (trade,) = Engine(m.history(), _condor(0.5, 1.0), FREE).run(DAY, DAY).trades
    assert {leg.exit_reason for leg in trade.legs} == {"time"}


def test_a_positional_trade_closes_at_its_days_to_expiry() -> None:
    # Expiry 25 Sep; exit at 3 days to expiry means the 22nd at the exit time.
    from optbt.strategies.legs import LegsConfig, LegSpec, LegStrategy

    expiry = date(2026, 9, 25)
    m = Market()
    for day in (DAY, date(2026, 9, 22)):
        m.index(day)
        for kind in (Kind.CALL, Kind.PUT):
            m.option(day, 23450.0, kind, 100.0, expiry=expiry)
    legs = (LegSpec(Side.SELL, Kind.CALL), LegSpec(Side.SELL, Kind.PUT))
    cfg = LegsConfig(legs=legs, hold="expiry", exit=time(15, 0), exit_dte=3)
    (trade,) = Engine(m.history(), LegStrategy(cfg), FREE).run(DAY, date(2026, 9, 22)).trades
    assert {leg.exit_reason for leg in trade.legs} == {"dte exit"}
    assert {leg.exit_ts for leg in trade.legs} == {datetime.combine(date(2026, 9, 22), time(15, 0))}


# ------------------------------------------------------------------ delta strikes


def _priced_chain(sigma: float, now: datetime, expiry: date, forward: float):  # type: ignore[no-untyped-def]
    """A chain priced by Black-76 at one known volatility, 50 points apart."""
    from analytics import black_scholes as bs
    from optbt.market import Quote

    years = (datetime.combine(expiry, time(15, 30)) - now).total_seconds() / (365 * 24 * 3600)
    chain = []
    for k in range(21000, 26050, 50):
        for kind, t in ((Kind.CALL, "CE"), (Kind.PUT, "PE")):
            price = bs.price(forward, k, 0.0, sigma, years, t)  # type: ignore[arg-type]
            chain.append(Quote(OptionKey(expiry, float(k), kind), round(price, 2), 100, 100))
    return chain, years


def test_delta_is_recovered_from_premiums_and_picks_the_nearest_strike() -> None:
    from analytics import black_scholes as bs
    from optbt.strategies.legs import StrikeRule, pick_strike, strike_deltas

    now = datetime(2026, 9, 11, 9, 59)
    expiry = date(2026, 10, 27)  # 46 days
    chain, years = _priced_chain(0.14, now, expiry, 23450.0)
    # The truth, straight from the volatility the chain was priced at.
    truth = {k: bs.greeks(23450.0, k, 0.0, 0.14, years, "CE").delta
             for k in range(21000, 26050, 50)}
    want = min(truth, key=lambda k: abs(truth[k] - 0.30))

    measured = strike_deltas(chain, 23450.0, Kind.CALL, now)
    assert measured[want] == pytest.approx(truth[want], abs=0.002)
    strike, _ = pick_strike(chain, 23450.0, Kind.CALL, StrikeRule(mode="delta", delta=0.30), now)
    assert strike == want

    put_truth = {k: -bs.greeks(23450.0, k, 0.0, 0.14, years, "PE").delta
                 for k in range(21000, 26050, 50)}
    put_want = min(put_truth, key=lambda k: abs(put_truth[k] - 0.17))
    put, _ = pick_strike(chain, 23450.0, Kind.PUT, StrikeRule(mode="delta", delta=0.17), now)
    assert put == put_want
    assert put < 23450 < strike  # a 0.17 put is below spot, a 0.30 call above


def test_a_delta_the_chain_does_not_reach_is_refused() -> None:
    from optbt.strategies.legs import StrikeRule, pick_strike

    now = datetime(2026, 9, 11, 9, 59)
    chain, _ = _priced_chain(0.14, now, date(2026, 10, 27), 23450.0)
    narrow = [q for q in chain if 23300 <= q.key.strike <= 23600]  # only near the money
    strike, why = pick_strike(narrow, 23450.0, Kind.CALL, StrikeRule(mode="delta", delta=0.05), now)
    assert strike is None and why == "no strike near that delta"


def test_no_expiry_near_the_days_asked_for_is_a_skip_not_a_short_trade() -> None:
    # Only 25 Sep is listed - 4 days out. A "45 DTE" trade must not take it.
    from optbt.strategies.legs import ExpiryChoice, LegsConfig, LegSpec, LegStrategy

    m = Market()
    m.index(DAY)
    near = date(2026, 9, 25)
    m.conn.execute("INSERT INTO expiry VALUES ('NIFTY', ?, 'futures')", [near])
    m.option(DAY, 23450.0, Kind.CALL, 100.0, expiry=near)
    leg = LegSpec(Side.SELL, Kind.CALL, expiry=ExpiryChoice("days", days=45))
    strategy = LegStrategy(LegsConfig(legs=(leg,), hold="expiry"))
    result = Engine(m.history(), strategy, FREE).run(DAY, DAY)
    assert result.trades == [] and result.skipped == {"no expiry listed": 1}


def test_a_leg_that_has_not_traded_today_is_valued_at_its_last_close() -> None:
    # A short call sold on the 21st at 100 and a long wing at 20. On the 22nd the
    # wing does not trade at all; the short jumps to 180. The position is -5,200
    # on the short, and the wing is still marked at 20, so a stop at the whole
    # 5,200 credit fires on the 22nd - not never, as it did when an untraded leg
    # left the position unvalued.
    from optbt.strategies.legs import LegsConfig, LegSpec, LegStrategy, StrikeRule

    m = Market()
    day2 = date(2026, 9, 22)
    later = date(2026, 9, 29)
    for d in (DAY, day2):
        m.index(d)
    for d, price in ((DAY, 100.0), (day2, 180.0)):
        m.option(d, 23450.0, Kind.CALL, price, expiry=later)
        m.option(d, 23450.0, Kind.PUT, 100.0, expiry=later)
    m.option(DAY, 23650.0, Kind.CALL, 20.0, expiry=later)  # the 21st only
    legs = (LegSpec(Side.SELL, Kind.CALL),
            LegSpec(Side.BUY, Kind.CALL, strike=StrikeRule(mode="premium", premium=20)))
    cfg = LegsConfig(legs=legs, hold="expiry", exit=time(23, 0), stop_credit=1.0)
    (trade,) = Engine(m.history(), LegStrategy(cfg), FREE).run(DAY, day2).trades
    short = next(leg for leg in trade.legs if leg.side is Side.SELL)
    assert short.exit_reason == "mtm stop" and short.exit_ts is not None
    assert short.exit_ts.date() == day2


# ----------------------------------------------------------------- adjustments


def _condor_days(level_day2: float):  # type: ignore[no-untyped-def]
    """A condor sold on the 21st with spot 23450: short CE 23550, long CE 23650,
    short PE 23350, long PE 23250 (strikes 50 apart). On the 22nd spot is at
    `level_day2`; every option sits at a flat price both days."""
    from optbt.strategies.legs import Adjustment, LegsConfig, LegSpec, LegStrategy, StrikeRule

    later = date(2026, 9, 29)
    day2 = date(2026, 9, 22)
    m = Market()
    m.index(DAY, 23450.0)
    m.index(day2, level_day2)
    for d in (DAY, day2):
        for k in STRIKES:
            for kind in (Kind.CALL, Kind.PUT):
                m.option(d, k, kind, 50.0, expiry=later)
    legs = (LegSpec(Side.SELL, Kind.CALL, strike=StrikeRule(offset=2)),
            LegSpec(Side.BUY, Kind.CALL, strike=StrikeRule(offset=4)),
            LegSpec(Side.SELL, Kind.PUT, strike=StrikeRule(offset=2)),
            LegSpec(Side.BUY, Kind.PUT, strike=StrikeRule(offset=4)))
    cfg = LegsConfig(legs=legs, hold="expiry", exit=time(23, 0), adjust=Adjustment(enabled=True))
    (trade,) = Engine(m.history(), LegStrategy(cfg), FREE).run(DAY, day2).trades
    return trade, day2


def test_a_fall_to_the_long_put_moves_the_call_spread_down() -> None:
    # 23290 is within 50 points of the 23250 long put. The 23550/23650 call
    # spread is closed and reopened with its short 200 points above the long
    # put - 23450 - and its wing the same 100 points above that, 23550.
    trade, day2 = _condor_days(23290.0)
    rolled = sorted((leg.key.strike, leg.side) for leg in trade.legs
                    if leg.exit_reason == "adjusted")
    assert rolled == [(23550.0, Side.SELL), (23650.0, Side.BUY)]
    new = sorted((leg.key.strike, leg.key.kind, leg.side) for leg in trade.legs
                 if leg.entry_ts.date() == day2)
    assert new == [(23450.0, Kind.CALL, Side.SELL), (23550.0, Kind.CALL, Side.BUY)]
    assert all(leg.entry_ts == datetime.combine(day2, time(9, 16))
               for leg in trade.legs if leg.entry_ts.date() == day2)


def test_a_rise_to_the_long_call_makes_it_an_iron_fly() -> None:
    # 23610 is within 50 points of the 23650 long call. The 23350/23250 put
    # spread moves up: its short to the short call's own strike, 23550, and its
    # wing the same 100 points below, 23450.
    trade, day2 = _condor_days(23610.0)
    shorts = sorted((leg.key.strike, leg.key.kind) for leg in trade.legs
                    if leg.is_open and leg.side is Side.SELL)
    assert shorts == [(23550.0, Kind.CALL), (23550.0, Kind.PUT)]
    longs = sorted((leg.key.strike, leg.key.kind) for leg in trade.legs
                   if leg.is_open and leg.side is Side.BUY)
    assert longs == [(23450.0, Kind.PUT), (23650.0, Kind.CALL)]


def test_equal_wings_set_both_sides_to_the_average_width() -> None:
    # Shorts ATM, wings OTM 2 on the call and OTM 4 on the put: widths 100 and
    # 200. Evened out, both become 150.
    from optbt.strategies.legs import LegsConfig, LegSpec, LegStrategy, StrikeRule

    m = _chain_day()
    legs = (LegSpec(Side.SELL, Kind.CALL),
            LegSpec(Side.BUY, Kind.CALL, strike=StrikeRule(offset=2)),
            LegSpec(Side.SELL, Kind.PUT),
            LegSpec(Side.BUY, Kind.PUT, strike=StrikeRule(offset=4)))
    cfg = LegsConfig(legs=legs, equal_wings=True)
    (trade,) = Engine(m.history(), LegStrategy(cfg), FREE).run(DAY, DAY).trades
    wings = sorted((leg.key.kind, leg.key.strike) for leg in trade.legs if leg.side is Side.BUY)
    assert wings == [(Kind.CALL, 23600.0), (Kind.PUT, 23300.0)]


def test_it_rolls_once_and_exits_stay_on_the_first_credit() -> None:
    from optbt.strategies.legs import _credit

    trade, _ = _condor_days(23290.0)
    assert sum(1 for leg in trade.legs if leg.exit_reason == "adjusted") == 2  # one spread
    # Four legs opened at 50: sold 2 x 50, bought 2 x 50 - a credit of 0 before
    # the roll. The new spread's premiums are not counted in it.
    assert _credit(trade.legs) == pytest.approx(0.0)


def test_nothing_happens_while_spot_stays_between_the_wings() -> None:
    trade, _ = _condor_days(23450.0)
    assert not any(leg.exit_reason == "adjusted" for leg in trade.legs)
    assert len(trade.legs) == 4


def _short_straddle_into_a_fall(*, put_last_trades: time | None) -> Market:
    """Day 1 at 23,450: the ATM call and put both at 100, sold at 09:20. Day 2 the
    index stands at 22,750 - from the open, or from 10:00 - and the call trades at
    5 from then on. The put, now 700 in the money, last traded at `put_last_trades`
    on day 2 (None: not at all) at its old 100, and next at 10:30, at 720."""
    day1, day2, expiry = date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 29)
    falls = time(9, 15) if put_last_trades is None else time(10, 0)
    m = Market()
    m.index(day1)
    minutes = _minutes(day2)
    rows = [
        f"('{INDEX}', '1', TIMESTAMP '{ts}', {p}, {p}, {p}, {p}, 0)"
        for ts in minutes
        for p in [22750.0 if ts.time() >= falls else 23450.0]
    ]
    m.conn.execute(f"INSERT INTO index_bar VALUES {','.join(rows)}")
    for strike in (23400.0, 23450.0, 23500.0):
        m.option(day1, strike, Kind.CALL, 100.0, expiry=expiry)
        m.option(day1, strike, Kind.PUT, 100.0, expiry=expiry)
    call = [
        f"('NIFTY', DATE '{expiry}', 'CE', 23450.0, TIMESTAMP '{ts}', {p}, {p}, {p}, {p}, "
        f"{LOT}, {LOT * 1000})"
        for ts in minutes
        for p in [5.0 if ts.time() >= falls else 100.0]
    ]
    put = [
        f"('NIFTY', DATE '{expiry}', 'PE', 23450.0, TIMESTAMP '{ts}', {p}, {p}, {p}, {p}, "
        f"{LOT}, {LOT * 1000})"
        for ts in minutes
        if (put_last_trades is not None and ts.time() <= put_last_trades)
        or ts.time() >= time(10, 30)
        for p in [100.0 if ts.time() < time(10, 30) else 720.0]
    ]
    m.conn.execute(f"INSERT INTO option_bar VALUES {','.join(call + put)}")
    return m


@pytest.mark.parametrize(
    "put_last_trades",
    [None, time(9, 59)],
    ids=["gap: the put has not traded today", "crash: the put last traded before it"],
)
def test_a_leg_that_has_not_traded_since_the_market_moved_is_not_valued_at_its_old_price(
    put_last_trades: time | None,
) -> None:
    """3 Feb and 13 Mar 2026. At its old 100 the put leaves the call's 95 points
    of profit standing - 6,175, past the 40%-of-credit target of 5,200 - and the
    target fires. Carried to an index 700 points lower it is worth over 700: the
    position is deep in loss and the target must not fire before the put trades."""
    from optbt.strategies.legs import LegsConfig, LegSpec, LegStrategy

    m = _short_straddle_into_a_fall(put_last_trades=put_last_trades)
    config = LegsConfig(
        legs=(LegSpec(Side.SELL, Kind.CALL), LegSpec(Side.SELL, Kind.PUT)),
        hold="expiry",
        target_credit=0.4,
    )
    (trade,) = Engine(m.history(), LegStrategy(config), FREE).run(
        date(2026, 9, 21), date(2026, 9, 22)
    ).trades
    assert all(leg.exit_reason != "mtm target" for leg in trade.legs), trade.events


# ------------------------------------------------------------- re-entry


def test_reentry_after_a_whole_position_stop_sells_again_at_the_new_price() -> None:
    """CE jumps from 100 to 120 at 10:00, a 1,300 loss that trips a 1,000 mtm
    stop: both legs are bought back at 10:01's open (120, 100). Re-entry sells
    the same legs again at 10:02's open, at those same now-flat prices - a
    fresh 220-point credit - and nothing moves again, so the second trade's
    own exit at 15:15 is at breakeven."""
    from optbt.strategies.legs import LegsConfig, LegSpec, LegStrategy, ReEntry

    m = _straddle_day(ce={time(10, 0): (100.0, 120.0, 100.0, 120.0)})
    config = LegsConfig(
        legs=(LegSpec(Side.SELL, Kind.CALL), LegSpec(Side.SELL, Kind.PUT)),
        mtm_stop=1000,
        reentry=ReEntry(enabled=True, trigger="mtm_stop", max_times=1),
    )
    result = Engine(m.history(), LegStrategy(config), FREE).run(DAY, DAY)
    assert len(result.trades) == 2
    first, second = result.trades
    assert first.reason == "mtm stop"
    assert first.net == pytest.approx(-1_300)
    assert [leg.entry_ts.time() for leg in second.legs] == [time(10, 2), time(10, 2)]
    assert sorted(leg.entry_price for leg in second.legs) == [100.0, 120.0]
    assert second.net == pytest.approx(0)


def test_reentry_stops_at_max_times() -> None:
    """The re-entered position trips the same mtm stop again at 11:00; with
    max_times=1 that does not start a third trade, so two trades in total."""
    from optbt.strategies.legs import LegsConfig, LegSpec, LegStrategy, ReEntry

    m = _straddle_day(
        ce={
            time(10, 0): (100.0, 120.0, 100.0, 120.0),
            time(11, 0): (120.0, 140.0, 120.0, 140.0),
        }
    )
    config = LegsConfig(
        legs=(LegSpec(Side.SELL, Kind.CALL), LegSpec(Side.SELL, Kind.PUT)),
        mtm_stop=1000,
        reentry=ReEntry(enabled=True, trigger="mtm_stop", max_times=1),
    )
    result = Engine(m.history(), LegStrategy(config), FREE).run(DAY, DAY)
    assert len(result.trades) == 2
    assert [t.reason for t in result.trades] == ["mtm stop", "mtm stop"]


def test_reentry_on_leg_stop_ignores_a_timed_exit() -> None:
    """trigger="leg_stop" only re-enters after a leg's own stop - not after a
    quiet day that simply timed out at the exit."""
    from optbt.strategies.legs import LegsConfig, LegSpec, LegStrategy, Level, ReEntry

    at_exit = {time(15, 15): (100.0, 100.0, 100.0, 100.0)}
    m = _straddle_day(ce=at_exit, pe=at_exit)
    config = LegsConfig(
        legs=(LegSpec(Side.SELL, Kind.CALL), LegSpec(Side.SELL, Kind.PUT)),
        reentry=ReEntry(enabled=True, trigger="leg_stop", max_times=2),
    )
    result = Engine(m.history(), LegStrategy(config), FREE).run(DAY, DAY)
    assert len(result.trades) == 1
    assert result.trades[0].reason == "time"


# ------------------------------------------------------------- entry triggers


def test_move_pct_trigger_waits_for_spot_to_move_from_its_price_at_entry() -> None:
    """Spot is flat at 23,450 through the morning, then jumps to 23,684.5 (+1%)
    at 10:00. With a 0.5% trigger the sale happens on the next bar, 10:01 - not
    at 09:20, where a fixed-time entry would have sold."""
    from optbt.strategies.legs import EntryTrigger, LegsConfig, LegSpec, LegStrategy

    m = Market()
    m.index(DAY, changes={time(10, 0): (23450.0, 23684.5, 23450.0, 23684.5)})
    for strike in (23400.0, 23450.0, 23500.0, 23700.0, 23650.0):
        m.option(DAY, strike, Kind.CALL, 100.0)
    m.option(DAY, 23450.0, Kind.PUT, 100.0)  # so the ATM strike can be found
    config = LegsConfig(
        legs=(LegSpec(Side.SELL, Kind.CALL),),
        trigger=EntryTrigger(mode="move_pct", move_pct=0.5),
    )
    (trade,) = Engine(m.history(), LegStrategy(config), FREE).run(DAY, DAY).trades
    assert trade.legs[0].entry_ts.time() == time(10, 1)


def test_range_breakout_trigger_waits_for_a_close_outside_the_opening_range() -> None:
    """09:20-09:35 wiggles between 23,440 and 23,460; at 09:35 it jumps to
    23,500, outside that range, and the sale happens on the next bar, 09:36."""
    from optbt.strategies.legs import EntryTrigger, LegsConfig, LegSpec, LegStrategy

    m = Market()
    m.index(
        DAY,
        changes={
            time(9, 25): (23450.0, 23460.0, 23450.0, 23455.0),
            time(9, 30): (23455.0, 23455.0, 23440.0, 23445.0),
            time(9, 35): (23445.0, 23500.0, 23445.0, 23500.0),
        },
    )
    for strike in (23400.0, 23450.0, 23500.0, 23550.0):
        m.option(DAY, strike, Kind.CALL, 100.0)
    m.option(DAY, 23450.0, Kind.PUT, 100.0)  # so the ATM strike can be found
    config = LegsConfig(
        legs=(LegSpec(Side.SELL, Kind.CALL),),
        trigger=EntryTrigger(mode="range_breakout", range_until=time(9, 35)),
    )
    (trade,) = Engine(m.history(), LegStrategy(config), FREE).run(DAY, DAY).trades
    assert trade.legs[0].entry_ts.time() == time(9, 36)


# ------------------------------------------------------------- strike modes


def test_straddle_width_strike_offsets_by_the_atm_straddles_premium() -> None:
    """ATM (23,450) call + put = 100 + 80 = 180. A call at width_mult 1 aims for
    23,450 + 180 = 23,630 and a put for 23,450 - 180 = 23,270 - both quoted, and
    picked over a further strike on each side."""
    from optbt.data.history import History
    from optbt.strategies.legs import StrikeRule, pick_strike

    m = Market()
    m.index(DAY)
    m.option(DAY, 23450.0, Kind.CALL, 100.0)
    m.option(DAY, 23450.0, Kind.PUT, 80.0)
    m.option(DAY, 23630.0, Kind.CALL, 20.0)
    m.option(DAY, 23750.0, Kind.CALL, 10.0)
    m.option(DAY, 23270.0, Kind.PUT, 15.0)
    m.option(DAY, 23150.0, Kind.PUT, 8.0)
    history: History = m.history()
    chain = history.chain_at(EXPIRY, datetime.combine(DAY, time(9, 20)))
    rule = StrikeRule(mode="straddle_width", width_mult=1.0)
    strike, why = pick_strike(chain, 23450.0, Kind.CALL, rule)
    assert (strike, why) == (23630.0, "")
    strike, why = pick_strike(chain, 23450.0, Kind.PUT, rule)
    assert (strike, why) == (23270.0, "")


def test_sp_pct_strike_targets_a_share_of_the_straddle_premium() -> None:
    """Straddle premium 180; 25% of that is 45, and the 23,600 call at 44 is
    the closest quoted premium to it."""
    from optbt.data.history import History
    from optbt.strategies.legs import StrikeRule, pick_strike

    m = Market()
    m.index(DAY)
    m.option(DAY, 23450.0, Kind.CALL, 100.0)
    m.option(DAY, 23450.0, Kind.PUT, 80.0)
    m.option(DAY, 23600.0, Kind.CALL, 44.0)
    m.option(DAY, 23650.0, Kind.CALL, 30.0)
    history: History = m.history()
    chain = history.chain_at(EXPIRY, datetime.combine(DAY, time(9, 20)))
    rule = StrikeRule(mode="sp_pct", sp_pct=25.0)
    strike, why = pick_strike(chain, 23450.0, Kind.CALL, rule)
    assert (strike, why) == (23600.0, "")


# ------------------------------------------------------- indicator signals


def _signal_market(changes: dict[time, tuple[float, float, float, float]]) -> Market:
    m = Market()
    m.index(DAY, changes=changes)
    for strike in (23400.0, 23450.0, 23500.0, 23550.0):
        m.option(DAY, strike, Kind.CALL, 100.0)
        m.option(DAY, strike, Kind.PUT, 100.0)
    return m


def _spot(op: str, level: float, timeframe: int = 1):  # type: ignore[no-untyped-def]
    from optbt.signals import Condition, Operand

    return Condition(Operand("price"), op, Operand("number", value=level), timeframe)  # type: ignore[arg-type]


#: Spot drops from 23,450 to 23,430 on the 10:00 bar and stays there.
DIP_AT_10 = {time(10, 0): (23450.0, 23450.0, 23430.0, 23430.0)}


def test_wait_enters_once_spot_is_below_a_level_rather_than_at_the_entry_time() -> None:
    """"Take the trade once price is below 23,440 again": nothing at 09:20 -
    spot is 23,450 - and no five-minute grace either. The 10:00 bar closes at
    23,430, so the sale fills in the 10:01 bar."""
    from optbt.signals import EntrySignal

    config = LegsConfig(
        legs=(LegSpec(Side.SELL, Kind.CALL),),
        entry_signal=EntrySignal(mode="wait", conditions=(_spot("below", 23440),)),
    )
    result = _run(_signal_market(DIP_AT_10), config)
    (trade,) = result.trades
    assert trade.legs[0].entry_ts.time() == time(10, 1)
    assert any("entry signal: spot 23,430.00 below 23440 on 1m" in e for e in trade.events)


def test_wait_on_a_five_minute_candle_waits_for_that_candle_to_finish() -> None:
    """The dip is inside the 10:00-10:04 candle, which finishes on the 10:04
    bar's close: the sale fills at 10:05, not 10:01."""
    from optbt.signals import EntrySignal

    config = LegsConfig(
        legs=(LegSpec(Side.SELL, Kind.CALL),),
        entry_signal=EntrySignal(mode="wait", conditions=(_spot("below", 23440, 5),)),
    )
    (trade,) = _run(_signal_market(DIP_AT_10), config).trades
    assert trade.legs[0].entry_ts.time() == time(10, 5)


def test_a_signal_that_never_comes_is_a_day_not_traded_and_counted() -> None:
    from optbt.signals import EntrySignal

    config = LegsConfig(
        legs=(LegSpec(Side.SELL, Kind.CALL),),
        entry_signal=EntrySignal(mode="wait", conditions=(_spot("below", 23000),)),
    )
    result = _run(_signal_market(DIP_AT_10), config)
    assert result.trades == []
    assert result.skipped == {"entry signal never came": 1}


@pytest.mark.parametrize(
    ("mode", "level", "trades", "skipped"),
    [
        ("take_if", 23400, 1, {}),
        ("take_if", 23500, 0, {"entry signal: not met": 1}),
        ("skip_if", 23400, 0, {"entry signal: met": 1}),
        ("skip_if", 23500, 1, {}),
    ],
)
def test_take_if_and_skip_if_judge_the_moment_of_entry(
    mode: str, level: float, trades: int, skipped: dict[str, int]
) -> None:
    """At 09:20 spot is 23,450: above 23,400, not above 23,500. Judged once -
    the dip later in the day does not bring a skipped entry back."""
    from optbt.signals import EntrySignal

    config = LegsConfig(
        legs=(LegSpec(Side.SELL, Kind.CALL),),
        entry_signal=EntrySignal(mode=mode, conditions=(_spot("above", level),)),  # type: ignore[arg-type]
    )
    result = _run(_signal_market(DIP_AT_10), config)
    assert len(result.trades) == trades
    assert result.skipped == skipped
    if trades:
        assert result.trades[0].legs[0].entry_ts.time() == time(9, 20)


def test_an_exit_signal_closes_the_position_on_the_next_bar() -> None:
    """Sold at 09:20; spot crosses below 23,440 on the 10:00 bar's close, so
    both legs are bought back in the 10:01 bar - long before 15:15."""
    from optbt.signals import ExitSignal
    from optbt.strategies.legs import straddle

    config = LegsConfig(
        legs=straddle(stop=None),
        exit_signal=ExitSignal(conditions=(_spot("crosses_below", 23440),)),
    )
    (trade,) = _run(_signal_market(DIP_AT_10), config).trades
    assert trade.reason == "signal exit"
    assert {leg.exit_ts.time() for leg in trade.legs if leg.exit_ts} == {time(10, 1)}
    assert any("exit signal: spot 23,430.00 crosses below 23440 on 1m" in e for e in trade.events)


def test_a_cross_from_before_the_entry_does_not_close_the_trade() -> None:
    """Waiting for spot below 23,440 and exiting on a cross below it: the cross
    that made the entry is not also its exit."""
    from optbt.signals import EntrySignal, ExitSignal

    config = LegsConfig(
        legs=(LegSpec(Side.SELL, Kind.CALL),),
        entry_signal=EntrySignal(mode="wait", conditions=(_spot("below", 23440),)),
        exit_signal=ExitSignal(conditions=(_spot("crosses_below", 23440),)),
    )
    (trade,) = _run(_signal_market(DIP_AT_10), config).trades
    assert trade.reason == "time"


def test_a_pivot_level_comes_from_the_previous_session() -> None:
    """Friday ranges 23,400-23,500 and closes at 23,450: P 23,450, R1 23,500.
    On Monday spot reaches 23,510 at 10:00, and a wait for spot above R1
    sells in the 10:01 bar."""
    from optbt.signals import Condition, EntrySignal, Operand

    friday = DAY - timedelta(days=3)
    m = _signal_market({time(10, 0): (23450.0, 23510.0, 23450.0, 23510.0)})
    m.index(
        friday,
        changes={
            time(11, 0): (23450.0, 23500.0, 23450.0, 23450.0),
            time(12, 0): (23450.0, 23450.0, 23400.0, 23450.0),
        },
    )
    above_r1 = Condition(Operand("price"), "above", Operand("level", level="R1"), 1)
    config = LegsConfig(
        legs=(LegSpec(Side.SELL, Kind.CALL),),
        entry_signal=EntrySignal(mode="wait", conditions=(above_r1,)),
    )
    (trade,) = _run(m, config).trades
    assert trade.legs[0].entry_ts.time() == time(10, 1)
    assert any("R1 23,500.00" in e for e in trade.events)


def test_a_reentry_waits_for_the_entry_signal_too() -> None:
    """The 10:00 stop-out comes with spot at 23,460 - above the 23,455 the
    entry waits to be below. Spot is back at 23,450 on the 11:00 bar, so the
    re-entry sells in the 11:01 bar rather than straight after the stop."""
    from optbt.signals import EntrySignal
    from optbt.strategies.legs import ReEntry

    m = Market()
    m.index(
        DAY,
        changes={
            time(10, 0): (23450.0, 23460.0, 23450.0, 23460.0),
            time(11, 0): (23460.0, 23460.0, 23450.0, 23450.0),
        },
    )
    for strike in (23400.0, 23450.0, 23500.0):
        ce = {time(10, 0): (100.0, 120.0, 100.0, 120.0)} if strike == 23450 else None
        m.option(DAY, strike, Kind.CALL, 100.0, changes=ce)
        m.option(DAY, strike, Kind.PUT, 100.0)
    config = LegsConfig(
        legs=(LegSpec(Side.SELL, Kind.CALL), LegSpec(Side.SELL, Kind.PUT)),
        mtm_stop=1000,
        reentry=ReEntry(enabled=True, trigger="mtm_stop", max_times=1),
        entry_signal=EntrySignal(mode="wait", conditions=(_spot("below", 23455),)),
    )
    first, second = _run(m, config).trades
    assert first.legs[0].entry_ts.time() == time(9, 20)
    assert first.reason == "mtm stop"
    assert [leg.entry_ts.time() for leg in second.legs] == [time(11, 1)] * 2
