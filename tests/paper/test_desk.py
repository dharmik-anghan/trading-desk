"""Paper trading against a market written down here: fills, fees, stops, expiry."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from api.store import open_db
from broker.shark.options_parse import (
    BasePair,
    BookLevel,
    Instrument,
    OptionTicker,
    OrderBook,
)
from paper import desk, fills
from paper.desk import PaperError
from paper.markets import SharkMarket
from storage import paper_repo

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
DELIVERY = datetime(2026, 10, 5, 8, 0, tzinfo=UTC)
SYMBOL = "BTC-5OCT26-85000-P-USDT"
INDEX = 85000.0

PAIR = BasePair(
    underlying="BTC",
    quote="USDT",
    maker_fee_pct=0.015,
    taker_fee_pct=0.02,
    fee_cap_pct=7,
    min_im_pct=5,
    max_im_pct=10,
    mm_pct=3,
    price_precision=2,
)


def book(bids: list[tuple[float, float]], asks: list[tuple[float, float]]) -> OrderBook:
    return OrderBook(
        SYMBOL,
        [BookLevel(p, q) for p, q in bids],
        [BookLevel(p, q) for p, q in asks],
    )


def ticker(bid: float | None, ask: float | None, mark: float) -> OptionTicker:
    return OptionTicker(
        symbol=SYMBOL,
        bid=bid,
        bid_size=1,
        ask=ask,
        ask_size=1,
        last=mark,
        mark=mark,
        mark_iv=0.4,
        bid_iv=None,
        ask_iv=None,
        delta=-0.5,
        gamma=0.0,
        vega=0.0,
        theta=0.0,
        index=INDEX,
        underlying=INDEX,
        open_interest=0,
        volume_24h=0,
        change_24h=0,
        received_at=NOW,
    )


class Market:
    """A market that says what it is told."""

    def __init__(self) -> None:
        self.book = book([(400, 0.5), (390, 1)], [(410, 0.3), (420, 1)])
        self.tick: OptionTicker | None = ticker(400, 410, 405)
        self.index: float | None = INDEX
        self.listed = Instrument(
            symbol=SYMBOL,
            underlying="BTC",
            quote="USDT",
            settle="INR",
            strike=85000,
            option_type="PE",
            delivery=DELIVERY,
            tick_size=5,
            qty_step=0.01,
            min_qty=0.01,
            max_qty=500,
            delivery_fee_rate=0.015,
            last=405,
            mark=405,
        )

    def order_book(self, symbol: str) -> OrderBook:
        return self.book

    def ticker(self, symbol: str) -> OptionTicker | None:
        return self.tick

    def spot(self, underlying: str) -> float | None:
        return self.index

    def base_pair(self, underlying: str) -> BasePair:
        return PAIR

    def instrument(self, symbol: str) -> Instrument | None:
        return self.listed if symbol == SYMBOL else None


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    return open_db(tmp_path / "paper.db")


@pytest.fixture
def fake() -> Market:
    return Market()


@pytest.fixture
def market(fake: Market) -> SharkMarket:
    return SharkMarket(fake)  # type: ignore[arg-type]


def markets(m: SharkMarket) -> dict[str, SharkMarket]:
    return {"shark_options": m}


@pytest.fixture
def session(conn: sqlite3.Connection) -> paper_repo.PaperSession:
    return paper_repo.create_session(conn, "test", "shark_options", "BTC")


class TestFills:
    def test_a_fill_walks_the_book_and_averages(self) -> None:
        levels = [BookLevel(410, 0.3), BookLevel(420, 1)]
        fill = fills.walk(levels, 0.5)
        assert fill.price == pytest.approx((410 * 0.3 + 420 * 0.2) / 0.5)
        assert fill.levels == 2

    def test_a_book_too_thin_is_refused_not_filled_at_a_guess(self) -> None:
        with pytest.raises(fills.NotEnoughBook, match="only 1.3"):
            fills.walk([BookLevel(410, 0.3), BookLevel(420, 1)], 2)
        with pytest.raises(fills.NotEnoughBook, match="nothing"):
            fills.walk([], 1)

    def test_a_buy_lifts_the_ask_and_a_sell_hits_the_bid(self) -> None:
        b = book([(400, 1)], [(410, 1)])
        assert fills.take(b, "buy", 0.1).price == 410
        assert fills.take(b, "sell", 0.1).price == 400

    def test_the_fee_is_a_share_of_notional_capped_by_premium(self) -> None:
        # 0.02% of 85,000 is 17 a coin, under 7% of a 400 premium (28).
        assert fills.trade_fee(PAIR, INDEX, 400, 1) == pytest.approx(17)
        # A 100 premium caps it at 7.
        assert fills.trade_fee(PAIR, INDEX, 100, 1) == pytest.approx(7)

    def test_expiring_worthless_costs_nothing_to_settle(self) -> None:
        assert fills.delivery_fee(PAIR, 0.015, INDEX, 0.0, 1) == 0
        assert fills.delivery_fee(PAIR, 0.015, INDEX, 500, 1) == pytest.approx(12.75)

    def test_settlement_is_intrinsic(self) -> None:
        assert fills.settle_value("PE", 85000, 84000) == 1000
        assert fills.settle_value("PE", 85000, 86000) == 0
        assert fills.settle_value("CE", 85000, 86000) == 1000

    def test_a_long_ties_up_nothing_and_a_short_its_im(self) -> None:
        args = {"kind": "PE", "strike": 80000.0, "index": INDEX, "mark": 100.0, "qty": 1.0}
        assert fills.margin(PAIR, side="buy", **args) == 0  # type: ignore[arg-type]
        # 10% of the index less 5,000 out of the money is 3,500; 5% is 4,250.
        assert fills.margin(PAIR, side="sell", **args) == pytest.approx(4250 + 100)  # type: ignore[arg-type]


class TestOrders:
    def test_a_sell_fills_at_the_bids_with_its_fee(
        self,
        conn: sqlite3.Connection,
        market: SharkMarket,
        fake: Market,
        session: paper_repo.PaperSession,
    ) -> None:
        leg = desk.open_leg(conn, market, session, SYMBOL, "sell", 0.8, NOW)
        assert leg.entry_price == pytest.approx((400 * 0.5 + 390 * 0.3) / 0.8)
        assert leg.entry_fee == pytest.approx(0.0002 * INDEX * 0.8)
        assert leg.delivery == DELIVERY
        assert leg.kind == "PE" and leg.strike == 85000

    @pytest.mark.parametrize(
        ("qty", "why"),
        [(0.005, "at least"), (0.015, "steps of"), (600, "over the venue")],
    )
    def test_a_quantity_the_venue_would_refuse_is_refused(
        self,
        conn: sqlite3.Connection,
        market: SharkMarket,
        session: paper_repo.PaperSession,
        qty: float,
        why: str,
    ) -> None:
        with pytest.raises(PaperError, match=why):
            desk.open_leg(conn, market, session, SYMBOL, "buy", qty, NOW)

    def test_an_expired_or_unknown_contract_is_refused(
        self,
        conn: sqlite3.Connection,
        market: SharkMarket,
        fake: Market,
        session: paper_repo.PaperSession,
    ) -> None:
        with pytest.raises(PaperError, match="expired"):
            desk.open_leg(conn, market, session, SYMBOL, "buy", 0.1, DELIVERY)
        with pytest.raises(PaperError, match="not listed"):
            desk.open_leg(conn, market, session, "BTC-5OCT26-1-P-USDT", "buy", 0.1, NOW)

    def test_an_exit_closes_on_the_other_side_once(
        self,
        conn: sqlite3.Connection,
        market: SharkMarket,
        fake: Market,
        session: paper_repo.PaperSession,
    ) -> None:
        leg = desk.open_leg(conn, market, session, SYMBOL, "sell", 0.1, NOW)
        closed = desk.exit_leg(conn, market, leg, NOW + timedelta(minutes=1))
        assert closed.exit_price == 410
        assert closed.exit_reason == "exit"
        with pytest.raises(PaperError, match="already closed"):
            desk.exit_leg(conn, market, closed, NOW)
        v = desk.value(market, closed, NOW)
        assert v.gross == pytest.approx(-(410 - 400) * 0.1)
        assert v.fees == pytest.approx(closed.entry_fee + (closed.exit_fee or 0))


class TestWatcher:
    def test_a_short_stops_when_the_ask_reaches_its_stop(
        self,
        conn: sqlite3.Connection,
        market: SharkMarket,
        fake: Market,
        session: paper_repo.PaperSession,
    ) -> None:
        leg = desk.open_leg(conn, market, session, SYMBOL, "sell", 0.1, NOW)
        paper_repo.set_levels(conn, leg.id, stop=450, target=None, enabled=True)
        assert desk.tick(conn, markets(market), NOW) == []
        fake.tick = ticker(440, 450, 445)
        fake.book = book([(440, 1)], [(450, 1)])
        assert desk.tick(conn, markets(market), NOW) == [f"stop {SYMBOL}"]
        closed = paper_repo.get_leg(conn, leg.id)
        assert closed is not None and closed.exit_price == 450 and closed.exit_reason == "stop"

    def test_a_long_takes_its_target_at_the_bid(
        self,
        conn: sqlite3.Connection,
        market: SharkMarket,
        fake: Market,
        session: paper_repo.PaperSession,
    ) -> None:
        leg = desk.open_leg(conn, market, session, SYMBOL, "buy", 0.1, NOW)
        paper_repo.set_levels(conn, leg.id, stop=None, target=500, enabled=True)
        fake.tick = ticker(500, 510, 505)
        fake.book = book([(500, 1)], [(510, 1)])
        assert desk.tick(conn, markets(market), NOW) == [f"target {SYMBOL}"]

    def test_no_quote_on_the_closing_side_fires_nothing(
        self,
        conn: sqlite3.Connection,
        market: SharkMarket,
        fake: Market,
        session: paper_repo.PaperSession,
    ) -> None:
        leg = desk.open_leg(conn, market, session, SYMBOL, "sell", 0.1, NOW)
        paper_repo.set_levels(conn, leg.id, stop=450, target=None, enabled=True)
        fake.tick = ticker(440, None, 600)
        assert desk.tick(conn, markets(market), NOW) == []

    def test_at_delivery_a_leg_settles_at_intrinsic(
        self,
        conn: sqlite3.Connection,
        market: SharkMarket,
        fake: Market,
        session: paper_repo.PaperSession,
    ) -> None:
        leg = desk.open_leg(conn, market, session, SYMBOL, "sell", 0.1, NOW)
        fake.index = 84000.0
        assert desk.tick(conn, markets(market), DELIVERY - timedelta(seconds=1)) == []
        assert desk.tick(conn, markets(market), DELIVERY) == [f"settled {SYMBOL}"]
        closed = paper_repo.get_leg(conn, leg.id)
        assert closed is not None
        assert closed.exit_reason == "expiry"
        assert closed.exit_price == 1000
        assert closed.exit_fee == pytest.approx(min(0.00015 * 84000 * 0.1, 0.07 * 1000 * 0.1))

    def test_the_sessions_rule_squares_everything_off_and_disarms(
        self,
        conn: sqlite3.Connection,
        market: SharkMarket,
        fake: Market,
        session: paper_repo.PaperSession,
    ) -> None:
        desk.open_leg(conn, market, session, SYMBOL, "sell", 0.5, NOW)
        paper_repo.set_rule(conn, session.id, stop=20, target=None)
        assert desk.tick(conn, markets(market), NOW) == []
        # The mark runs up 60 a coin: 30 on half a coin, past the 20 stop.
        fake.tick = ticker(455, 465, 460)
        fake.book = book([(455, 1)], [(465, 1)])
        assert desk.tick(conn, markets(market), NOW) == [f"portfolio stop on session {session.id}"]
        assert all(not leg.open for leg in paper_repo.legs(conn, session.id))
        after = paper_repo.get_session(conn, session.id)
        assert after is not None
        assert after.rule_stop is None
        assert after.squared_reason == "portfolio stop"

    def test_a_leg_left_out_is_not_counted_or_closed_by_the_rule(
        self,
        conn: sqlite3.Connection,
        market: SharkMarket,
        fake: Market,
        session: paper_repo.PaperSession,
    ) -> None:
        leg = desk.open_leg(conn, market, session, SYMBOL, "sell", 0.5, NOW)
        paper_repo.set_levels(conn, leg.id, stop=None, target=None, enabled=False)
        paper_repo.set_rule(conn, session.id, stop=20, target=None)
        fake.tick = ticker(455, 465, 460)
        assert desk.tick(conn, markets(market), NOW) == []
        still = paper_repo.get_leg(conn, leg.id)
        assert still is not None and still.open

    def test_two_closers_cannot_both_book_an_exit(
        self,
        conn: sqlite3.Connection,
        market: SharkMarket,
        fake: Market,
        session: paper_repo.PaperSession,
    ) -> None:
        leg = desk.open_leg(conn, market, session, SYMBOL, "sell", 0.1, NOW)
        assert paper_repo.close_leg(conn, leg.id, at=NOW, price=1, fee=0, reason="a")
        assert not paper_repo.close_leg(conn, leg.id, at=NOW, price=2, fee=0, reason="b")
        closed = paper_repo.get_leg(conn, leg.id)
        assert closed is not None and replace(closed).exit_reason == "a"
