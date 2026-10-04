"""Shark's options market, read against what the live service actually sent.

`fixtures/shark/options_*.json` were captured from the public endpoints and the
public socket on 2026-10-04: the nearest BTC expiry's catalogue, the expiry list,
one order book, and a full set of ticker frames for that expiry.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from broker.errors import BrokerError
from broker.models import Quote
from broker.shark.options import SharkOptionsBroker, split_underlying
from broker.shark.options_feed import OptionsFeed, book_topic, chain_topic
from broker.shark.options_parse import (
    SYMBOLS,
    expiry_code,
    parse_base_pairs,
    parse_delivery_times,
    parse_expiry_code,
    parse_instruments,
    parse_option_ticker,
    parse_order_book,
    parse_symbol,
    to_expiries,
)

FIXTURES = Path(__file__).parent / "fixtures" / "shark"
EXPIRY = date(2026, 10, 5)


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def fresh(frame: dict[str, Any]) -> dict[str, Any]:
    return dict(frame)


class TestSymbols:
    def test_expiry_codes_round_trip(self) -> None:
        assert expiry_code(EXPIRY) == "5OCT26"
        assert expiry_code(date(2027, 6, 25)) == "25JUN27"
        assert parse_expiry_code("5OCT26") == EXPIRY
        assert parse_expiry_code("25JUN27") == date(2027, 6, 25)

    def test_a_malformed_expiry_is_not_a_date(self) -> None:
        for bad in ("", "5OC26", "32OCT26", "XXOCT26", "5OCTXX"):
            assert parse_expiry_code(bad) is None

    def test_a_contract_symbol_reads_into_its_parts(self) -> None:
        name = parse_symbol("BTC-5OCT26-85500-P-USDT")
        assert name is not None
        assert (name.underlying, name.expiry, name.strike, name.option_type, name.quote) == (
            "BTC",
            EXPIRY,
            85500.0,
            "PE",
            "USDT",
        )
        assert name.series == "BTC-5OCT26"

    def test_an_underlying_is_not_a_contract(self) -> None:
        assert parse_symbol("BTCUSDT") is None
        assert parse_symbol("BTC-5OCT26-85500-X-USDT") is None

    def test_the_codec_finds_a_contracts_expiry(self) -> None:
        expiries = to_expiries(parse_delivery_times(load("options_delivery_times.json")))
        found = SYMBOLS.expiry_for_symbol("BTC-5OCT26-85500-C-USDT", expiries)
        assert found is not None and found.date == "05-10-2026"
        assert SYMBOLS.parse_contract("BTC-5OCT26-85500-C-USDT") == ("BTC-5OCT26", 85500.0, "CE")
        assert SYMBOLS.series_prefix("BTC-5OCT26-85500-C-USDT", 85500.0) == "BTC-5OCT26"
        assert SYMBOLS.futures_symbol("BTC-5OCT26-85500-C-USDT", 85500.0) is None

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("BTC", ("BTC", "USDT")), ("btcusdt", ("BTC", "USDT")), ("ETH-USDT", ("ETH", "USDT"))],
    )
    def test_an_underlying_can_be_named_several_ways(
        self, raw: str, expected: tuple[str, str]
    ) -> None:
        assert split_underlying(raw) == expected


class TestCatalogue:
    def test_reads_every_contract_in_the_expiry(self) -> None:
        instruments = parse_instruments(load("options_instruments.json"))
        assert len(instruments) == 58
        assert {i.option_type for i in instruments} == {"CE", "PE"}
        first = instruments[0]
        assert first.delivery == datetime(2026, 10, 5, 8, 0, tzinfo=UTC)
        assert first.settle == "INR"
        assert first.qty_step == 0.01
        assert first.min_qty == 0.01

    def test_expiries_mark_the_months_last_as_monthly(self) -> None:
        expiries = to_expiries(parse_delivery_times(load("options_delivery_times.json")))
        assert len(expiries) == 12
        assert expiries[0].date == "05-10-2026"
        assert expiries[0].token == "1791187200000"
        monthly = [e.date for e in expiries if not e.weekly]
        # One per month listed, and each the last of its month.
        assert len(monthly) == len({e.date[3:] for e in expiries})
        for m in monthly:
            same_month = [e.date for e in expiries if e.date[3:] == m[3:]]
            assert m == max(same_month, key=lambda d: int(d[:2]))

    def test_base_pairs_carry_fee_and_margin_terms(self) -> None:
        pairs = {p.underlying: p for p in parse_base_pairs(load("options_base_pairs.json"))}
        assert set(pairs) == {"BTC", "ETH"}
        btc = pairs["BTC"]
        assert btc.taker_fee_pct == 0.02
        assert btc.fee_cap_pct == 7
        assert btc.mm_pct == 3


class TestFrames:
    def test_a_ticker_frame_carries_prices_and_greeks(self) -> None:
        frame = next(
            f
            for f in load("options_stream.json")["ticker"]
            if f["symbol"] == "BTC-5OCT26-85500-P-USDT"
        )
        ticker = parse_option_ticker(frame)
        assert ticker is not None
        assert ticker.bid is not None and ticker.ask is not None
        assert ticker.bid < ticker.ask
        assert ticker.mark is not None and ticker.underlying is not None
        greeks = ticker.greeks
        assert greeks is not None
        assert -1 < greeks.delta < 0  # a put
        assert 0 < greeks.iv < 5  # a fraction, not a percentage

    def test_an_empty_side_is_absent_not_zero(self) -> None:
        frame = dict(load("options_stream.json")["ticker"][0], bidPrice="0", askPrice="")
        ticker = parse_option_ticker(frame)
        assert ticker is not None
        assert ticker.bid is None and ticker.ask is None

    def test_a_frame_without_a_symbol_is_dropped(self) -> None:
        assert parse_option_ticker({"bidPrice": "1"}) is None
        assert parse_option_ticker("nonsense") is None

    def test_the_book_is_best_first_whichever_way_it_arrives(self) -> None:
        rest = parse_order_book(load("options_order_book.json"))
        assert [lv.price for lv in rest.bids] == sorted(
            (lv.price for lv in rest.bids), reverse=True
        )
        assert [lv.price for lv in rest.asks] == sorted(lv.price for lv in rest.asks)
        # The socket sends asks highest first.
        raw = load("options_order_book.json")
        flipped = parse_order_book(dict(raw, asks=list(reversed(raw["asks"]))))
        assert flipped.asks == rest.asks
        assert rest.bids[0].price < rest.asks[0].price


def fed() -> OptionsFeed:
    feed = OptionsFeed()
    stream = load("options_stream.json")
    for frame in stream["ticker"]:
        feed.receive_ticker(frame)
    feed.receive_underlying(stream["underlying"])
    feed.receive_index(stream["indexPrice"])
    return feed


class TestFeed:
    def test_holds_the_latest_frame_per_contract(self) -> None:
        feed = fed()
        assert len(feed.tickers("BTC-5OCT26-")) == 58
        assert feed.tickers("ETH-") == []
        held = feed.underlying("BTC", "USDT", EXPIRY)
        assert held is not None and held[0] > 0
        assert feed.index("BTC", "USDT") is not None

    def test_listeners_hear_each_ticker(self) -> None:
        heard: list[str] = []
        feed = OptionsFeed()
        feed.on_ticker = lambda t: heard.append(t.symbol)
        feed.receive_ticker(load("options_stream.json")["ticker"][0])
        assert len(heard) == 1

    def test_a_topic_nothing_asks_for_goes_idle(self) -> None:
        feed = OptionsFeed(idle_seconds=10)
        topic = chain_topic("BTC", "USDT", EXPIRY)
        assert topic == "BTC_USDT_5OCT26@ticker"
        # Not connected: wanting a topic records it, to subscribe on connect.
        feed.want([topic, book_topic("BTC-5OCT26-85500-P-USDT")])
        asked = feed._wanted[topic]
        assert feed.idle_topics(now=asked + 5) == []
        assert set(feed.idle_topics(now=asked + 11)) == {topic, "BTC-5OCT26-85500-P-USDT@orderBook"}


class FakeResponse:
    def __init__(self, body: Any, status: int = 200) -> None:
        self._body = body
        self.status_code = status
        self.text = json.dumps(body)

    def json(self) -> Any:
        return self._body


class FakeSession:
    """Serves the captured REST responses by path."""

    ROUTES = {
        "/v1/exchange/basePairs": "options_base_pairs.json",
        "/v1/exchange/delivery-times": "options_delivery_times.json",
        "/v1/exchange/instrument-info": "options_instruments.json",
        "/v1/market/orderBook": "options_order_book.json",
    }

    def __init__(self) -> None:
        self.paths: list[str] = []

    def get(self, url: str, params: Any = None, timeout: float = 0) -> FakeResponse:
        path = url.split(".in", 1)[1]
        self.paths.append(path)
        name = self.ROUTES.get(path)
        if name is None:
            return FakeResponse({"message": f"Cannot GET {path}"}, 404)
        return FakeResponse(load(name))


class FakePerps:
    def __init__(self, price: float | None = 85000.0) -> None:
        self.price = price

    def get_quote(self, symbols: list[str]) -> dict[str, Quote]:
        if self.price is None:
            return {}
        now = datetime.now(UTC)
        p = self.price
        return {
            s: Quote(
                symbol=s,
                ltp=p,
                open=p,
                high=p,
                low=p,
                prev_close=p,
                volume=0,
                bid=p,
                ask=p,
                timestamp=now,
            )
            for s in symbols
        }


def broker(feed: OptionsFeed | None, perps: FakePerps | None = None) -> SharkOptionsBroker:
    return SharkOptionsBroker(
        feed,
        session=FakeSession(),
        perps=perps or FakePerps(),  # type: ignore[arg-type]
        first_frames_wait=0,
    )


class TestChain:
    def test_centres_on_the_expirys_futures_price(self) -> None:
        feed = fed()
        chain = broker(feed).get_option_chain("BTC", strike_count=3)
        forward = feed.underlying("BTC", "USDT", EXPIRY)
        assert forward is not None
        assert chain.underlying_ltp == forward[0]
        strikes = sorted({r.strike for r in chain.rows})
        assert len(strikes) == 7
        atm = min(strikes, key=lambda k: abs(k - forward[0]))
        assert strikes[3] == atm
        assert chain.expiry_token == "1791187200000"
        assert len(chain.expiries) == 12

    def test_rows_carry_the_live_book_mark_and_greeks(self) -> None:
        chain = broker(fed()).get_option_chain("BTC", strike_count=2)
        assert len(chain.rows) == 10
        for row in chain.rows:
            assert row.mark is not None and row.mark > 0
            assert row.greeks is not None
        assert any(r.bid > 0 and r.ask > r.bid for r in chain.rows)

    def test_stale_frames_are_not_served_as_live(self) -> None:
        feed = fed()
        old = datetime.now(UTC) - timedelta(minutes=5)
        for symbol, ticker in list(feed._tickers.items()):
            feed._tickers[symbol] = type(ticker)(**{**ticker.__dict__, "received_at": old})
        chain = broker(feed).get_option_chain("BTC", strike_count=1)
        assert all(r.greeks is None and r.bid == 0 for r in chain.rows)

    def test_without_a_feed_the_catalogue_and_perpetual_stand_in(self) -> None:
        chain = broker(None, FakePerps(85000.0)).get_option_chain("BTC", strike_count=1)
        assert chain.underlying_ltp == 85000.0
        assert all(r.greeks is None for r in chain.rows)
        assert any(r.ltp > 0 for r in chain.rows)

    def test_no_price_anywhere_is_an_error_not_a_zero(self) -> None:
        with pytest.raises(BrokerError, match="no BTC price"):
            broker(None, FakePerps(None)).get_option_chain("BTC")

    def test_an_unknown_expiry_token_falls_back_to_the_nearest(self) -> None:
        chain = broker(fed()).get_option_chain("BTC", strike_count=1, expiry_token="123")
        assert chain.expiry_token == "1791187200000"


class TestQuotes:
    def test_a_contract_is_quoted_from_the_feed(self) -> None:
        symbol = "BTC-5OCT26-85500-P-USDT"
        quote = broker(fed()).get_quote([symbol])[symbol]
        assert quote.bid > 0 and quote.ask > quote.bid

    def test_an_underlying_is_quoted_as_its_index(self) -> None:
        feed = fed()
        index = feed.index("BTC", "USDT")
        assert index is not None
        assert broker(feed).get_quote(["BTC"])["BTC"].ltp == index[0]

    def test_option_history_is_refused(self) -> None:
        with pytest.raises(BrokerError, match="no candle history"):
            broker(fed()).get_history("BTC-5OCT26-85500-P-USDT", "1", EXPIRY, EXPIRY)

    def test_the_book_comes_from_the_feed_once_it_carries_it(self) -> None:
        feed = fed()
        session = FakeSession()
        adapter = SharkOptionsBroker(feed, session=session, perps=FakePerps(), first_frames_wait=0)  # type: ignore[arg-type]
        symbol = "BTC-5OCT26-85500-P-USDT"
        adapter.order_book(symbol)
        assert session.paths == ["/v1/market/orderBook"]
        feed.receive_book(load("options_order_book.json"))
        adapter.order_book(symbol)
        assert session.paths == ["/v1/market/orderBook"]
