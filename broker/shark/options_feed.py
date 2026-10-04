"""Shark's live options feed: the latest state of every contract asked about.

The options REST service lists contracts and order books but serves no prices
beyond a stale snapshot in its catalogue - no ticker, no greeks, no index. Those
only exist on the socket (`fawss-options`), which is what the venue's own app
draws its chain from. So a chain is read from here: a process-wide cache the
socket keeps current, which a synchronous adapter reads without waiting on I/O.

Topics, as the venue's web app names them:

- `BTC_USDT_5OCT26@ticker`: every contract in one expiry, about one full
  snapshot per contract per second;
- `BTC_USDT_5OCT26@underlying`: the futures price that expiry is priced off;
- `BTC_USDT@indexPrice`: the spot index;
- `<contract>@orderBook`: one contract's resting orders.

Subscriptions are made on demand and dropped once nothing has asked for them in
`IDLE_SECONDS`. All of one underlying's expiries at once is ~700 contracts at a
frame a second each, which is a lot of parsing for a chain nobody is looking at.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
import time as clock
from collections.abc import Callable, Iterable
from concurrent.futures import Future
from datetime import UTC, date, datetime
from typing import Any

import aiohttp
import certifi
import socketio

from broker.shark.options_parse import (
    OptionTicker,
    OrderBook,
    parse_expiry_code,
    parse_option_ticker,
    parse_order_book,
    topic_prefix,
)
from broker.shark.parse import SharkParseError

log = logging.getLogger(__name__)

STREAM_URL = "https://fawss-options.sharkexchange.in"

#: How long a topic stays subscribed after the last read that wanted it.
IDLE_SECONDS = 600.0

#: How often idle topics are looked for.
SWEEP_SECONDS = 60.0


def chain_topic(underlying: str, quote: str, expiry: date) -> str:
    return f"{topic_prefix(underlying, quote, expiry)}@ticker"


def underlying_topic(underlying: str, quote: str, expiry: date) -> str:
    return f"{topic_prefix(underlying, quote, expiry)}@underlying"


def index_topic(underlying: str, quote: str) -> str:
    return f"{underlying}_{quote}@indexPrice"


def book_topic(symbol: str) -> str:
    return f"{symbol}@orderBook"


class OptionsFeed:
    """The options socket, and the latest frame of each kind it has carried.

    Started and stopped on the app's event loop like the perpetuals stream. Read
    from any thread: `want()` asks the loop to subscribe and returns at once, and
    the readers return what has arrived so far.
    """

    def __init__(self, url: str = STREAM_URL, *, idle_seconds: float = IDLE_SECONDS) -> None:
        self._url = url
        self._idle = idle_seconds
        self._sio: socketio.AsyncClient | None = None
        self._session: aiohttp.ClientSession | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sweeper: asyncio.Task[None] | None = None
        #: Topic -> when something last asked for it, on the monotonic clock.
        self._wanted: dict[str, float] = {}
        self._subscribed: set[str] = set()
        self._tickers: dict[str, OptionTicker] = {}
        #: (underlying, quote, expiry) -> futures price, and when it came.
        self._underlying: dict[tuple[str, str, date], tuple[float, datetime]] = {}
        self._index: dict[tuple[str, str], tuple[float, datetime]] = {}
        self._books: dict[str, tuple[OrderBook, datetime]] = {}
        self.connected = False
        self.frames_received = 0
        #: Called with each parsed ticker - how a recorder or a runner listens in.
        self.on_ticker: Callable[[OptionTicker], None] | None = None

    # -------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        """Connect. Topics are subscribed as they are asked for."""
        self._loop = asyncio.get_running_loop()
        context = ssl.create_default_context(cafile=certifi.where())
        self._session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=context))
        sio = socketio.AsyncClient(
            http_session=self._session, reconnection=True, reconnection_delay=2
        )
        self._sio = sio
        sio.on("connect", self._on_connect)
        sio.on("disconnect", self._on_disconnect)
        sio.on("ticker", self._on_ticker)
        sio.on("underlying", self._on_underlying)
        sio.on("indexPrice", self._on_index)
        sio.on("orderBook", self._on_book)
        await sio.connect(self._url, transports=["websocket"])
        self._sweeper = asyncio.create_task(self._sweep_forever(), name="shark-options-sweep")

    async def stop(self) -> None:
        if self._sweeper is not None:
            self._sweeper.cancel()
            self._sweeper = None
        if self._sio is not None:
            await self._sio.disconnect()
            self._sio = None
        if self._session is not None:
            await self._session.close()
            self._session = None
        self.connected = False
        self._subscribed.clear()

    # ---------------------------------------------------------- subscriptions

    def want(self, topics: Iterable[str]) -> None:
        """Keep these topics subscribed, subscribing any that are not yet.

        Safe from any thread. Returns without waiting: the first frames land a
        moment later, and a caller that needs them uses `wait_until`.
        """
        now = clock.monotonic()
        fresh = []
        for t in topics:
            if t not in self._wanted:
                fresh.append(t)
            self._wanted[t] = now
        if fresh and self._loop is not None and self.connected:
            self._submit(self._subscribe(fresh))

    def _submit(self, coro: Any) -> Future[None] | None:
        assert self._loop is not None
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            self._loop.create_task(coro)
            return None
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    async def _subscribe(self, topics: list[str]) -> None:
        topics = [t for t in topics if t not in self._subscribed]
        if not topics or self._sio is None:
            return
        await self._sio.emit("subscribe", {"params": topics})
        self._subscribed.update(topics)
        log.info("shark options subscribed to %s", ", ".join(topics))

    async def _unsubscribe(self, topics: list[str]) -> None:
        if not topics or self._sio is None:
            return
        await self._sio.emit("unsubscribe", {"params": topics})
        self._subscribed.difference_update(topics)
        log.info("shark options dropped %s", ", ".join(topics))

    def idle_topics(self, now: float | None = None) -> list[str]:
        """Topics nothing has asked for within the idle window."""
        at = clock.monotonic() if now is None else now
        return [t for t, asked in self._wanted.items() if at - asked > self._idle]

    async def _sweep_forever(self) -> None:
        while True:
            await asyncio.sleep(SWEEP_SECONDS)
            idle = self.idle_topics()
            for t in idle:
                self._wanted.pop(t, None)
            await self._unsubscribe([t for t in idle if t in self._subscribed])

    # --------------------------------------------------------------- handlers

    async def _on_connect(self) -> None:
        """Resubscribe on every connect: a reconnect starts with none."""
        self.connected = True
        self._subscribed.clear()
        await self._subscribe(list(self._wanted))

    async def _on_disconnect(self) -> None:
        self.connected = False
        log.warning("shark options feed disconnected")

    async def _on_ticker(self, payload: Any) -> None:
        self.receive_ticker(payload)

    async def _on_underlying(self, payload: Any) -> None:
        self.receive_underlying(payload)

    async def _on_index(self, payload: Any) -> None:
        self.receive_index(payload)

    async def _on_book(self, payload: Any) -> None:
        self.receive_book(payload)

    # Plain methods so tests, and a replay, can feed frames without a socket.

    def receive_ticker(self, payload: Any) -> None:
        ticker = parse_option_ticker(payload)
        if ticker is None:
            return
        self.frames_received += 1
        self._tickers[ticker.symbol] = ticker
        if self.on_ticker is not None:
            self.on_ticker(ticker)

    def receive_underlying(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        expiry = parse_expiry_code(str(payload.get("expireTime", "")))
        price = _positive(payload.get("underlyingPrice"))
        if expiry is None or price is None:
            return
        key = (str(payload.get("baseCoin")), str(payload.get("quoteCoin")), expiry)
        self._underlying[key] = (price, datetime.now(UTC))

    def receive_index(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        price = _positive(payload.get("indexPrice"))
        if price is None:
            return
        key = (str(payload.get("baseCoin")), str(payload.get("quoteCoin")))
        self._index[key] = (price, datetime.now(UTC))

    def receive_book(self, payload: Any) -> None:
        try:
            book = parse_order_book(payload)
        except SharkParseError:
            return
        self._books[book.symbol] = (book, datetime.now(UTC))

    # ---------------------------------------------------------------- readers

    def tickers(self, symbol_prefix: str) -> list[OptionTicker]:
        """Every contract whose symbol starts with this - `BTC-5OCT26-` is one expiry."""
        return [t for s, t in list(self._tickers.items()) if s.startswith(symbol_prefix)]

    def ticker(self, symbol: str) -> OptionTicker | None:
        return self._tickers.get(symbol)

    def underlying(
        self, underlying: str, quote: str, expiry: date
    ) -> tuple[float, datetime] | None:
        return self._underlying.get((underlying, quote, expiry))

    def index(self, underlying: str, quote: str) -> tuple[float, datetime] | None:
        return self._index.get((underlying, quote))

    def book(self, symbol: str) -> tuple[OrderBook, datetime] | None:
        return self._books.get(symbol)

    def wait_until(self, ready: Callable[[], bool], timeout: float) -> bool:
        """Block the calling thread until `ready()` or the timeout. Not on the loop."""
        deadline = clock.monotonic() + timeout
        while not ready():
            if clock.monotonic() >= deadline:
                return False
            clock.sleep(0.05)
        return True


def _positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None
