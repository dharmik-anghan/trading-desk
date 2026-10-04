"""The NSE market behind the paper desk, and templates placed on a live chain."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from api.store import open_db
from broker.fyers import SYMBOLS
from optbt.spec import from_dict
from optbt.strategies.legs import ExpiryChoice
from optbt.templates import TEMPLATES
from paper import desk
from paper.markets import NseMarket, PaperError
from paper.resolve import pick_live_expiry, resolve
from storage import paper_repo
from tests.paper.fakes import LOT, FakeFyers, symbol

#: Friday 2 Oct 2026, 10:00 IST: in session.
OPEN = datetime(2026, 10, 2, 4, 30, tzinfo=UTC)
#: The same day, 20:00 IST.
SHUT = datetime(2026, 10, 2, 14, 30, tzinfo=UTC)


def nse(fake: FakeFyers | None = None) -> NseMarket:
    f = fake or FakeFyers()
    return NseMarket(f, SYMBOLS, f, clock=lambda: OPEN)  # type: ignore[arg-type]


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    return open_db(tmp_path / "paper.db")


class TestNseChain:
    def test_reads_the_lot_off_the_chain(self) -> None:
        chain = nse().chain("NIFTY", "", 15)
        assert chain.step == LOT and chain.min_qty == LOT
        assert chain.currency == "INR"
        assert chain.expiry.expiry.isoformat() == "2026-10-06"
        assert chain.expiry.delivery.astimezone().hour is not None
        # 15:30 IST is 10:00 UTC.
        assert chain.expiry.delivery.astimezone(UTC).hour == 10

    def test_prices_each_strike_from_its_mid(self) -> None:
        chain = nse().chain("NIFTY", "", 15)
        row = next(r for r in chain.rows if r.strike == 25000)
        assert row.ce is not None
        _, q = row.ce
        assert q.bid is not None and q.ask is not None
        assert q.mark == pytest.approx((q.bid + q.ask) / 2)
        # No greeks from the broker: solved from the price.
        assert q.iv is not None and 0 < q.iv < 2
        assert q.delta is not None and 0.3 < q.delta < 0.7

    def test_an_unseen_contract_is_found_through_its_expiry(self) -> None:
        market = nse()
        found = market.contract(symbol(25100, "PE"))
        assert found is not None
        assert (found.strike, found.kind, found.step) == (25100, "PE", LOT)

    def test_fills_take_the_top_of_the_book(self) -> None:
        market = nse()
        market.chain("NIFTY", "", 15)
        s = symbol(25000, "CE")
        q = market.quote(s)
        assert q is not None
        assert market.fill(s, "buy", LOT) == q.ask
        assert market.fill(s, "sell", LOT) == q.bid

    def test_charges_are_the_backtests(self) -> None:
        market = nse()
        # Brokerage alone is 20 an order; taxes come on top.
        assert market.fee(symbol(25000, "CE"), 100.0, LOT, buy=True, at=OPEN) > 20

    def test_the_session_decides_what_can_trade(self, conn: sqlite3.Connection) -> None:
        market = nse()
        session = paper_repo.create_session(conn, "t", "fyers", "NIFTY")
        assert market.is_open(OPEN) and not market.is_open(SHUT)
        with pytest.raises(PaperError, match="closed"):
            desk.open_leg(conn, market, session, symbol(25000, "CE"), "sell", LOT, SHUT)
        with pytest.raises(PaperError, match="steps of 65"):
            desk.open_leg(conn, market, session, symbol(25000, "CE"), "sell", 50, OPEN)
        leg = desk.open_leg(conn, market, session, symbol(25000, "CE"), "sell", 2 * LOT, OPEN)
        assert leg.qty == 130 and leg.entry_fee > 0


class TestNseSettlement:
    def test_a_short_call_settles_at_intrinsic_on_the_close(self, conn: sqlite3.Connection) -> None:
        fake = FakeFyers()
        market = nse(fake)
        session = paper_repo.create_session(conn, "t", "fyers", "NIFTY")
        leg = desk.open_leg(conn, market, session, symbol(24950, "CE"), "sell", LOT, OPEN)
        fake.spot = 25100.0
        at_close = leg.delivery
        assert desk.tick(conn, {"fyers": market}, at_close) == [f"settled {leg.symbol}"]
        closed = paper_repo.get_leg(conn, leg.id)
        assert closed is not None
        assert closed.exit_price == 150
        # A short pays no exercise STT.
        assert closed.exit_fee == 0


class TestExpiryChoice:
    def test_weekly_on_crypto_is_the_fridays(self) -> None:
        from paper.markets import LiveExpiry

        days = [datetime(2026, 10, d, 8, tzinfo=UTC) for d in (5, 6, 9, 16, 30)]
        listed = [
            LiveExpiry(t.date(), str(i), d == 30, t)
            for i, (t, d) in enumerate(zip(days, (5, 6, 9, 16, 30), strict=True))
        ]
        now = datetime(2026, 10, 4, 12, tzinfo=UTC)
        daily = pick_live_expiry(listed, ExpiryChoice("daily", 1), now, fridays_weekly=True)
        weekly = pick_live_expiry(listed, ExpiryChoice("weekly", 2), now, fridays_weekly=True)
        monthly = pick_live_expiry(listed, ExpiryChoice("monthly", 1), now, fridays_weekly=True)
        assert daily is not None and daily.expiry.day == 5
        assert weekly is not None and weekly.expiry.day == 16
        assert monthly is not None and monthly.expiry.day == 30
        assert pick_live_expiry(listed, ExpiryChoice("weekly", 9), now, fridays_weekly=True) is None
        # A day's minimum skips tomorrow's.
        skip = pick_live_expiry(
            listed, ExpiryChoice("daily", 1, min_left=2), now, fridays_weekly=True
        )
        assert skip is not None and skip.expiry.day == 6


class TestTemplates:
    def resolved(self, template_id: str) -> list:  # type: ignore[type-arg]
        market = nse()
        spec = next(t.spec for t in TEMPLATES if t.id == template_id)
        first = market.chain("NIFTY", "", 40)
        return resolve(
            from_dict(spec),
            {first.expiry.token: first},
            first.expiries,
            OPEN,
            fridays_weekly=False,
            load=lambda token: market.chain("NIFTY", token, 40),
        )

    def test_a_strangle_lands_two_strikes_out(self) -> None:
        legs = self.resolved("short_strangle")
        assert [(d.side, d.kind, d.strike) for d in legs] == [
            ("sell", "CE", 25100),
            ("sell", "PE", 24900),
        ]
        assert all(d.qty == LOT and d.error is None for d in legs)
        assert all(d.stop is not None and d.stop.value == 0.25 for d in legs)

    def test_a_condor_has_its_wings_further_out(self) -> None:
        legs = self.resolved("iron_condor")
        strikes = [(d.side, d.kind, d.strike) for d in legs]
        assert strikes == [
            ("sell", "CE", 25200),
            ("buy", "CE", 25400),
            ("sell", "PE", 24800),
            ("buy", "PE", 24600),
        ]

    def test_the_45_dte_condor_takes_the_monthly_by_delta(self) -> None:
        legs = self.resolved("condor_45dte")
        # The only monthly is 25 days out: inside the backtest's slack around 45.
        assert {d.expiry.isoformat() for d in legs if d.expiry} == {"2026-10-27"}
        assert all(d.error is None for d in legs)
        short_call, long_call, short_put, long_put = (d.strike for d in legs)
        assert long_call > short_call > 25010 > short_put > long_put
