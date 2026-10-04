"""Real orders behind a live session - against a fake account, never Shark's."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from api.store import open_db
from broker.shark.options_account import OptionsOrder
from paper import desk
from paper.live import Limits, LiveError, SharkExecutor, check
from paper.markets import Execution, PaperError, SharkMarket
from storage import paper_repo
from tests.paper.test_desk import NOW, SYMBOL, Market

LIMITS = Limits(enabled=True, max_notional=2000.0, daily_loss=50.0)


class FakeAccount:
    """Fills, refuses or sits on orders as told, and remembers every one."""

    def __init__(
        self, status: str = "FILLED", avg: float = 401.0, filled: float | None = None
    ) -> None:
        self.status = status
        self.avg = avg
        self.filled = filled
        self.placed: list[tuple[str, str, float]] = []
        self.cancelled: list[str] = []

    def _order(self, n: int) -> OptionsOrder:
        symbol, side, qty = self.placed[n]
        status = "CANCELED" if f"o{n}" in self.cancelled and self.status == "NEW" else self.status
        filled = self.filled if self.filled is not None else (qty if status == "FILLED" else 0.0)
        return OptionsOrder(
            f"o{n}",
            symbol,
            side,  # type: ignore[arg-type]
            status,
            qty,
            filled,
            self.avg if filled else None,
        )

    def place_market(self, symbol: str, side: str, qty: float) -> OptionsOrder:
        self.placed.append((symbol, side, qty))
        return OptionsOrder(f"o{len(self.placed) - 1}", symbol, side, "NEW", qty, 0.0, None)  # type: ignore[arg-type]

    def recent_orders(self) -> list[OptionsOrder]:
        return [self._order(n) for n in range(len(self.placed))]

    def cancel(self, order_id: str) -> None:
        self.cancelled.append(order_id)


def executor(account: FakeAccount, market: SharkMarket) -> SharkExecutor:
    return SharkExecutor(account, market, wait=0.05, sleep=lambda _: None)  # type: ignore[arg-type]


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    return open_db(tmp_path / "paper.db")


@pytest.fixture
def fake() -> Market:
    return Market()


@pytest.fixture
def market(fake: Market) -> SharkMarket:
    return SharkMarket(fake)  # type: ignore[arg-type]


@pytest.fixture
def live(conn: sqlite3.Connection) -> paper_repo.PaperSession:
    return paper_repo.create_session(conn, "real", "shark_options", "BTC", "live")


class TestExecutor:
    def test_a_filled_order_reports_the_venues_price(self, market: SharkMarket) -> None:
        account = FakeAccount(avg=402.5)
        done = executor(account, market).execute(SYMBOL, "sell", 0.1, NOW)
        assert (done.price, done.qty, done.order_id) == (402.5, 0.1, "o0")
        assert done.fee > 0
        assert account.placed == [(SYMBOL, "sell", 0.1)]

    def test_a_refused_order_says_nothing_filled(self, market: SharkMarket) -> None:
        with pytest.raises(LiveError, match="rejected .* nothing filled"):
            executor(FakeAccount(status="REJECTED"), market).execute(SYMBOL, "buy", 0.1, NOW)

    def test_an_order_that_sits_is_cancelled(self, market: SharkMarket) -> None:
        account = FakeAccount(status="NEW")
        with pytest.raises(LiveError, match="did not fill and was cancelled"):
            executor(account, market).execute(SYMBOL, "buy", 0.1, NOW)
        assert account.cancelled == ["o0"]

    def test_a_part_fill_says_what_is_open(self, market: SharkMarket) -> None:
        account = FakeAccount(status="PARTIALLY_FILLED", filled=0.04)
        with pytest.raises(LiveError, match="only 0.04 of 0.1 .* open on Shark"):
            executor(account, market).execute(SYMBOL, "buy", 0.1, NOW)


class TestChecks:
    def priced(
        self, qty: float = 0.01, side: str = "sell", price: float = 400.0
    ) -> list[desk.Priced]:
        return [desk.Priced(SYMBOL, side, qty, price, 1.0)]  # type: ignore[arg-type]

    def test_live_off_refuses_everything(
        self, conn: sqlite3.Connection, market: SharkMarket
    ) -> None:
        off = Limits(enabled=False, max_notional=2000, daily_loss=50)
        with pytest.raises(PaperError, match="SHARK_OPTIONS_LIVE"):
            check(conn, market, off, self.priced(), "BTC", NOW)

    def test_a_leg_over_the_notional_cap_is_refused(
        self, conn: sqlite3.Connection, market: SharkMarket
    ) -> None:
        # 0.03 BTC at 85,000 is 2,550 of notional.
        with pytest.raises(PaperError, match="over the 2,000 cap"):
            check(conn, market, LIMITS, self.priced(0.03), "BTC", NOW)
        check(conn, market, LIMITS, self.priced(0.02), "BTC", NOW)

    def test_the_wallet_must_cover_the_margin(
        self, conn: sqlite3.Connection, market: SharkMarket
    ) -> None:
        with pytest.raises(PaperError, match="options wallet has 10.00 free"):
            check(conn, market, LIMITS, self.priced(0.02), "BTC", NOW, available=lambda: 10.0)
        check(conn, market, LIMITS, self.priced(0.02), "BTC", NOW, available=lambda: 10_000.0)

    def test_todays_live_losses_stop_new_orders(
        self,
        conn: sqlite3.Connection,
        market: SharkMarket,
        fake: Market,
        live: paper_repo.PaperSession,
    ) -> None:
        # A short sold at 400 that is now marked at 1,000, on 0.1: down 60.
        leg = desk.open_leg(conn, market, live, SYMBOL, "sell", 0.1, NOW)
        paper_repo.close_leg(conn, leg.id, at=NOW, price=1000.0, fee=0.0, reason="exit")
        with pytest.raises(PaperError, match="past the 50.00 daily limit"):
            check(conn, market, LIMITS, self.priced(), "BTC", NOW + timedelta(minutes=1))
        # Paper losses do not count.
        paper = paper_repo.create_session(conn, "p", "shark_options", "BTC")
        assert paper.mode == "paper"


class TestLiveDesk:
    def test_a_basket_books_the_venues_fills_with_their_orders(
        self, conn: sqlite3.Connection, market: SharkMarket, live: paper_repo.PaperSession
    ) -> None:
        priced = [desk.price_order(market, live, SYMBOL, "sell", 0.1, NOW)]
        legs, problem = desk.open_live(
            conn, market, executor(FakeAccount(avg=399.0), market), live, priced, NOW
        )
        assert problem is None
        stored = paper_repo.get_leg(conn, legs[0].id)
        assert stored is not None
        assert (stored.entry_price, stored.entry_order) == (399.0, "o0")

    def test_a_basket_that_breaks_part_way_keeps_what_filled(
        self, conn: sqlite3.Connection, market: SharkMarket, live: paper_repo.PaperSession
    ) -> None:
        class SecondRefused:
            venue = "shark_options"
            n = 0

            def execute(self, symbol: str, side: Any, qty: float, now: datetime) -> Execution:
                self.n += 1
                if self.n == 2:
                    raise LiveError("Shark rejected the second; nothing filled")
                return Execution(400.0, qty, 1.0, f"o{self.n}")

        priced = [
            desk.price_order(market, live, SYMBOL, side, 0.1, NOW) for side in ("sell", "buy")
        ]
        legs, problem = desk.open_live(conn, market, SecondRefused(), live, priced, NOW)
        assert len(legs) == 1
        assert problem is not None and "1 of 2 legs were filled" in problem

    def test_a_live_stop_sends_a_real_order_and_needs_an_executor(
        self,
        conn: sqlite3.Connection,
        market: SharkMarket,
        fake: Market,
        live: paper_repo.PaperSession,
    ) -> None:
        from tests.paper.test_desk import book, ticker

        leg = desk.open_leg(conn, market, live, SYMBOL, "sell", 0.1, NOW)
        paper_repo.set_levels(conn, leg.id, stop=450, target=None, enabled=True)
        fake.tick = ticker(440, 450, 445)
        fake.book = book([(440, 1)], [(450, 1)])
        markets = {"shark_options": market}
        # Live trading off: a real position is not closed on paper.
        assert desk.tick(conn, markets, NOW) == []
        account = FakeAccount(avg=451.0)
        assert desk.tick(conn, markets, NOW, {"shark_options": executor(account, market)}) == [
            f"stop {SYMBOL}"
        ]
        closed = paper_repo.get_leg(conn, leg.id)
        assert closed is not None
        assert (closed.exit_price, closed.exit_order) == (451.0, "o0")
        assert account.placed == [(SYMBOL, "buy", 0.1)]


def test_days_are_counted_in_ist(conn: sqlite3.Connection, market: SharkMarket) -> None:
    # 23:00 UTC on the 3rd is 04:30 IST on the 4th: a new day for the limit.
    late = datetime(2026, 10, 3, 23, 0, tzinfo=UTC)
    check(conn, market, LIMITS, [desk.Priced(SYMBOL, "sell", 0.01, 400.0, 1.0)], "BTC", late)
