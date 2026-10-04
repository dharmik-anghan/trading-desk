"""The Shark Exchange options adapter: BTC and ETH options, settled in INR.

A separate service from the perpetuals one (`api-options.` rather than `api.`),
and a separate adapter for it, because the two share no endpoints and almost no
vocabulary. Implements `MarketData` and `OptionsData`; not `Trading` yet.

Where each thing comes from:

- the contract list, expiries and order books: REST, public;
- prices, greeks, the index and the futures price an expiry is priced off: the
  socket only, read through `OptionsFeed`. Without a running feed (a script, a
  test) the chain falls back to the catalogue's own prices, which are a stale
  snapshot - fine for listing strikes, not for trading off.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from time import monotonic
from typing import Any

import requests

from broker.errors import BrokerError, BrokerUnreachable, classify_status
from broker.models import Candle, Expiry, OptionChain, OptionChainRow, Quote
from broker.shark.options_feed import (
    OptionsFeed,
    book_topic,
    chain_topic,
    index_topic,
    underlying_topic,
)
from broker.shark.options_parse import (
    BasePair,
    Instrument,
    OptionTicker,
    OrderBook,
    expiry_code,
    parse_base_pairs,
    parse_delivery_times,
    parse_instruments,
    parse_order_book,
    parse_symbol,
    to_expiries,
)
from broker.shark.rest import SharkBroker

log = logging.getLogger(__name__)

BASE_URL = "https://api-options.sharkexchange.in"
TIMEOUT = 20.0
DEFAULT_QUOTE = "USDT"

#: The catalogue changes when the venue lists a new expiry - daily - and the
#: base pairs' terms more rarely still.
INSTRUMENTS_TTL = 300.0
BASE_PAIRS_TTL = 3600.0

#: How long a chain read waits for the socket's first frames on an expiry it had
#: not subscribed to yet. A frame per contract arrives about once a second.
FIRST_FRAMES_WAIT = 3.0

#: A ticker older than this is the socket having stopped, not a quiet market:
#: the venue sends a snapshot per contract every second whether it traded or not.
STALE_SECONDS = 30.0


def split_underlying(symbol: str) -> tuple[str, str]:
    """`"BTC"`, `"BTCUSDT"` or `"BTC-USDT"` -> `("BTC", "USDT")`."""
    raw = symbol.upper().replace("_", "-")
    if "-" in raw:
        base, quote = raw.split("-", 1)
        return base, quote
    if raw.endswith(DEFAULT_QUOTE) and len(raw) > len(DEFAULT_QUOTE):
        return raw[: -len(DEFAULT_QUOTE)], DEFAULT_QUOTE
    return raw, DEFAULT_QUOTE


class SharkOptionsBroker:
    """Shark's options market. Public data only: no key is needed to read it."""

    def __init__(
        self,
        feed: OptionsFeed | None = None,
        *,
        base_url: str = BASE_URL,
        session: requests.Session | None = None,
        perps: SharkBroker | None = None,
        first_frames_wait: float = FIRST_FRAMES_WAIT,
    ) -> None:
        self._feed = feed
        self._base = base_url.rstrip("/")
        self._session = session or requests.Session()
        # The underlying's candles come from the perpetuals service, whose kline
        # endpoint is public: the options one serves none at all.
        self._perps = perps or SharkBroker(api_key="", api_secret="", session=self._session)
        self._wait = first_frames_wait
        self._instruments: dict[tuple[str, str], tuple[float, list[Instrument]]] = {}
        self._pairs: tuple[float, list[BasePair]] | None = None
        self._rate: tuple[float, float] | None = None

    # ---------------------------------------------------------------- plumbing

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            response = self._session.get(f"{self._base}{path}", params=params, timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise BrokerUnreachable(f"cannot reach Shark options ({type(exc).__name__})") from exc
        if response.status_code >= 400:
            try:
                body = response.json()
                message = body.get("message") or body.get("details") or response.text[:120]
            except ValueError:
                message = response.text[:120]
            text = str(message)
            raise classify_status(response.status_code, text)(f"Shark options: {text}")
        try:
            return response.json()
        except ValueError as exc:
            raise BrokerError(
                f"Shark options returned a non-JSON body: {response.text[:120]}"
            ) from exc

    # --------------------------------------------------------------- catalogue

    def base_pairs(self) -> list[BasePair]:
        """The underlyings options are listed on, with their fee and margin terms."""
        if self._pairs is None or monotonic() - self._pairs[0] > BASE_PAIRS_TTL:
            self._pairs = (monotonic(), parse_base_pairs(self._get("/v1/exchange/basePairs")))
        return self._pairs[1]

    def conversion_rate(self) -> float:
        """Rupees a USDT, as the venue converts the INR wallet to margin a
        USDT-priced option. From `/v1/exchange/meta`, held for five minutes."""
        if self._rate is None or monotonic() - self._rate[0] > INSTRUMENTS_TTL:
            meta = self._get("/v1/exchange/meta")
            rate = float(meta.get("conversionRate", 0)) if isinstance(meta, dict) else 0.0
            if rate <= 0:
                raise BrokerError("Shark sent no INR conversion rate")
            self._rate = (monotonic(), rate)
        return self._rate[1]

    def base_pair(self, underlying: str) -> BasePair:
        base, quote = split_underlying(underlying)
        for pair in self.base_pairs():
            if pair.underlying == base and pair.quote == quote:
                return pair
        raise BrokerError(f"Shark lists no options on {base}-{quote}")

    def instruments(self, underlying: str) -> list[Instrument]:
        """Every listed contract on the underlying, all expiries."""
        key = split_underlying(underlying)
        held = self._instruments.get(key)
        if held is None or monotonic() - held[0] > INSTRUMENTS_TTL:
            base, quote = key
            rows = self._get("/v1/exchange/instrument-info", {"baseCoin": base, "quoteCoin": quote})
            held = self._instruments[key] = (monotonic(), parse_instruments(rows))
        return held[1]

    def instrument(self, symbol: str) -> Instrument | None:
        name = parse_symbol(symbol)
        if name is None:
            return None
        return next(
            (i for i in self.instruments(f"{name.underlying}-{name.quote}") if i.symbol == symbol),
            None,
        )

    def expiries(self, underlying: str) -> list[Expiry]:
        base, quote = split_underlying(underlying)
        rows = self._get("/v1/exchange/delivery-times", {"baseCoin": base, "quoteCoin": quote})
        return to_expiries(parse_delivery_times(rows))

    # ------------------------------------------------------------ market data

    def order_book(self, symbol: str) -> OrderBook:
        """One contract's resting orders - the socket's, if it is carrying them.

        Asking also keeps the book subscribed, so a caller polling a contract it
        is about to trade reads it from memory after the first time.
        """
        if self._feed is not None:
            self._feed.want([book_topic(symbol)])
            held = self._feed.book(symbol)
            if held is not None and _fresh(held[1]):
                return held[0]
        return parse_order_book(self._get("/v1/market/orderBook", {"symbol": symbol}))

    def ticker(self, symbol: str) -> OptionTicker | None:
        """The socket's latest state for one contract, or None if it has none fresh."""
        if self._feed is None:
            return None
        name = parse_symbol(symbol)
        if name is None:
            return None
        self._feed.want([chain_topic(name.underlying, name.quote, name.expiry)])
        ticker = self._feed.ticker(symbol)
        return ticker if ticker is not None and _fresh(ticker.received_at) else None

    def spot(self, underlying: str) -> float | None:
        """The spot index, from the socket."""
        if self._feed is None:
            return None
        base, quote = split_underlying(underlying)
        self._feed.want([index_topic(base, quote)])
        held = self._feed.index(base, quote)
        return held[0] if held is not None and _fresh(held[1]) else None

    def get_option_chain(
        self, symbol: str, strike_count: int = 10, expiry_token: str = ""
    ) -> OptionChain:
        """One expiry's chain: `strike_count` strikes either side of the money.

        `symbol` is the underlying ("BTC"). Priced off the futures price for the
        expiry, which is what the venue prices its options off - the spot index
        differs from it by the basis, and on a 3-month expiry that is real money.
        """
        base, quote = split_underlying(symbol)
        expiries = self.expiries(f"{base}-{quote}")
        if not expiries:
            raise BrokerError(f"Shark lists no {base} expiries")
        chosen = (
            next((e for e in expiries if e.token == expiry_token), None) if expiry_token else None
        )
        if chosen is None:
            chosen = expiries[0]
        delivery = datetime.fromtimestamp(int(chosen.token) / 1000, tz=UTC)
        expiry_day = delivery.date()
        listed = [i for i in self.instruments(f"{base}-{quote}") if i.delivery == delivery]

        tickers = self._live_tickers(base, quote, expiry_day, len(listed))
        forward = self._forward(base, quote, expiry_day, tickers)
        if forward is None:
            raise BrokerError(f"no {base} price yet - the options feed has not carried one")

        strikes = sorted({i.strike for i in listed})
        atm = min(strikes, key=lambda k: abs(k - forward)) if strikes else forward
        below = [k for k in strikes if k < atm][-strike_count:]
        above = [k for k in strikes if k > atm][:strike_count]
        keep = set(below) | {atm} | set(above)

        rows = [
            _row(i, tickers.get(i.symbol))
            for i in sorted(listed, key=lambda i: (i.strike, i.option_type))
            if i.strike in keep
        ]
        return OptionChain(
            underlying_symbol=f"{base}-{quote}",
            underlying_ltp=forward,
            fetched_at=datetime.now(UTC),
            rows=rows,
            expiries=expiries,
            expiry_token=chosen.token,
            call_oi=int(sum(r.oi for r in rows if r.option_type == "CE")),
            put_oi=int(sum(r.oi for r in rows if r.option_type == "PE")),
        )

    def _live_tickers(
        self, base: str, quote: str, expiry: date, listed: int
    ) -> dict[str, OptionTicker]:
        feed = self._feed
        if feed is None:
            return {}
        feed.want(
            [
                chain_topic(base, quote, expiry),
                underlying_topic(base, quote, expiry),
                index_topic(base, quote),
            ]
        )
        prefix = f"{base}-{expiry_code(expiry)}-"

        def enough() -> bool:
            # Most of the chain, not all: a contract listed minutes ago may not
            # have ticked yet, and waiting for it would stall every read.
            return len(feed.tickers(prefix)) >= max(1, int(listed * 0.8))

        if feed.connected and not enough():
            feed.wait_until(enough, self._wait)
        return {t.symbol: t for t in feed.tickers(prefix) if _fresh(t.received_at)}

    def _forward(
        self, base: str, quote: str, expiry: date, tickers: dict[str, OptionTicker]
    ) -> float | None:
        if self._feed is not None:
            held = self._feed.underlying(base, quote, expiry)
            if held is not None and _fresh(held[1]):
                return held[0]
        for t in tickers.values():
            if t.underlying is not None:
                return t.underlying
        spot = self.spot(f"{base}-{quote}")
        if spot is not None:
            return spot
        # No feed at all: the perpetual's last price is the nearest thing to it.
        try:
            quote_now = self._perps.get_quote([f"{base}{quote}"])
        except BrokerError:
            return None
        found = quote_now.get(f"{base}{quote}")
        return found.ltp if found is not None else None

    def get_quote(self, symbols: list[str]) -> dict[str, Quote]:
        """Option contracts from the socket, an underlying ("BTC") as its index."""
        out: dict[str, Quote] = {}
        now = datetime.now(UTC)
        for symbol in symbols:
            name = parse_symbol(symbol)
            if name is None:
                price = self.spot(symbol)
                if price is None:
                    base, quote = split_underlying(symbol)
                    perp = self._perps.get_quote([f"{base}{quote}"]).get(f"{base}{quote}")
                    if perp is None:
                        continue
                    out[symbol] = perp.model_copy(update={"symbol": symbol})
                    continue
                out[symbol] = Quote(
                    symbol=symbol,
                    ltp=price,
                    open=price,
                    high=price,
                    low=price,
                    prev_close=price,
                    volume=0.0,
                    bid=price,
                    ask=price,
                    timestamp=now,
                )
                continue
            ticker = self.ticker(symbol)
            if ticker is not None:
                price = ticker.last or ticker.mark or 0.0
                out[symbol] = Quote(
                    symbol=symbol,
                    ltp=price,
                    open=price - ticker.change_24h,
                    high=price,
                    low=price,
                    prev_close=price - ticker.change_24h,
                    volume=ticker.volume_24h,
                    bid=ticker.bid or 0.0,
                    ask=ticker.ask or 0.0,
                    timestamp=ticker.received_at,
                )
                continue
            listed = self.instrument(symbol)
            if listed is None:
                continue
            price = listed.last or listed.mark or 0.0
            out[symbol] = Quote(
                symbol=symbol,
                ltp=price,
                open=price,
                high=price,
                low=price,
                prev_close=price,
                volume=0.0,
                bid=0.0,
                ask=0.0,
                timestamp=now,
            )
        return out

    def get_history(
        self, symbol: str, resolution: str, date_from: date, date_to: date
    ) -> list[Candle]:
        """The underlying's candles, from its perpetual. Options have none to give."""
        if parse_symbol(symbol) is not None:
            raise BrokerError("Shark serves no candle history for option contracts")
        base, quote = split_underlying(symbol)
        return self._perps.get_history(f"{base}{quote}", resolution, date_from, date_to)


def _fresh(at: datetime) -> bool:
    return (datetime.now(UTC) - at).total_seconds() <= STALE_SECONDS


def _row(instrument: Instrument, ticker: OptionTicker | None) -> OptionChainRow:
    if ticker is None:
        price = instrument.last or instrument.mark or 0.0
        return OptionChainRow(
            symbol=instrument.symbol,
            strike=instrument.strike,
            option_type=instrument.option_type,
            ltp=price,
            bid=0.0,
            ask=0.0,
            oi=0,
            prev_oi=0,
            volume=0,
            mark=instrument.mark,
        )
    price = ticker.last or ticker.mark or 0.0
    before = price - ticker.change_24h
    return OptionChainRow(
        symbol=instrument.symbol,
        strike=instrument.strike,
        option_type=instrument.option_type,
        ltp=price,
        bid=ticker.bid or 0.0,
        ask=ticker.ask or 0.0,
        oi=int(ticker.open_interest),
        prev_oi=0,
        volume=int(ticker.volume_24h),
        ltp_change=ticker.change_24h,
        ltp_change_pct=(ticker.change_24h / before * 100) if before > 0 else 0.0,
        greeks=ticker.greeks,
        mark=ticker.mark,
    )
