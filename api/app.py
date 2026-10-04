"""Dashboard API: composition only.

This module used to hold every endpoint and every helper they shared, which was
manageable while the desk had one venue and one asset class. It no longer is, so
the endpoints live in `api/routers/`, grouped by what they are about, and what
is left here is assembly: the app, its middleware, the broker error handler, the
routers, and the built frontend.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import date
from functools import partial
from pathlib import Path
from types import FrameType
from typing import Any, cast

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.types import Scope

import paths
from api.alert_inputs import gather
from api.deps import (
    get_broker,
    get_db_path,
    get_executors,
    get_feeds,
    get_holidays,
    get_paper_markets,
)
from api.errors import broker_error_handler
from api.routers import (
    alerts,
    backtest,
    bars,
    baskets,
    chart,
    crypto_options,
    feeds,
    live,
    market,
    optbt,
    perps,
    portfolio,
    preopen,
    rrg,
    simulator,
    strategies,
    structure,
    system,
    volatility,
)
from api.store import open_db
from broker.base import AsyncStreaming, OptionsBroker
from broker.errors import BrokerError
from broker.factory import (
    account_stream_for,
    broker_for,
    chain_feed_for,
    codec_for,
    expired_source_for,
    invalidate_account,
    is_configured,
    stream_for,
)
from broker.factory import options_broker as default_options_broker
from broker.fyers.account_stream import AccountEvent
from jobs.alert_watcher import Watcher
from jobs.daily_bars import DailyBarUpdater
from jobs.option_backfill import OptionBackfiller
from jobs.paper_watcher import PaperWatcher
from jobs.preopen_recorder import PreOpenRecorder
from jobs.vol_recorder import VolRecorder
from marketdata import BarService, nse_preopen
from marketdata.holder import BarStoreHolder
from marketdata.models import Bar
from marketdata.venue import VenueBars, to_bar
from notify import Telegram, TelegramConfig
from optbt.data.store import OptionStore
from settings import load_settings
from streaming import TickHub
from streaming.account import AccountHub
from universe.nse import daily_series
from venues import AssetClass, Capability, listed, listed_on, option_underlyings, serving
from venues.calendar import in_session

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Run the alert watcher for as long as the app is up.

    Alerts used to be computed in the browser, so nothing was watching when the
    tab was closed. This is the task that fixes that, and it is the reason the
    app has a lifespan at all - the venues' tick streams hang off the same hook.

    Everything is best-effort: if the watcher cannot be built, the desk still
    serves. A trading screen that refuses to start because a notifier is
    misconfigured is worse than one that starts and says alerts are off, which is
    what `/api/alerts` reports.
    """
    settings = load_settings()
    notifier = (
        Telegram(
            TelegramConfig(
                bot_token=settings.telegram_bot_token,
                chat_id=settings.telegram_chat_id,
            )
        )
        if settings.has_telegram
        else None
    )

    def options_broker() -> OptionsBroker:
        """The options broker as a request would get it, overrides included, so a
        test's fake reaches the background jobs too rather than a live account."""
        override = application.dependency_overrides.get(get_broker)
        return cast(OptionsBroker, override()) if override else default_options_broker()

    db_path = get_db_path()
    # Make sure the schema is current before the watcher's first pass, which runs
    # on a worker thread and would otherwise race the first request to do it.
    open_db(db_path).close()

    feeds_cache = get_feeds()
    # Built before the watcher so price watches on streamed instruments read from
    # it rather than asking a broker that does not list them.
    hub = TickHub()

    # Set when the app is going down, so a long-lived response can end itself. An
    # SSE stream loops until its reader leaves, and on shutdown the reader has not
    # left - uvicorn waits for the response to finish while the response waits for
    # the reader. That hung a reload with a desk open, and would hang `docker stop`.
    shutting_down = asyncio.Event()
    application.state.shutting_down = shutting_down
    _set_on_stop_signal(shutting_down)

    # The bar store, and every venue that serves history registered as a source
    # under its own name. The venue's candles are what a trading chart shows - the
    # instrument an order would be in - and storing them is what turns a few
    # hundred bars into a history.
    #
    # Both venues, not just the perpetuals one. Registering only Shark meant the
    # options desk's chart had no live source at all: `BarService` found nothing
    # mapped to "fyers", so every request fell through to whatever the offline
    # backfill had written - and since that script needs the desk stopped to take
    # the store's write lock, the chart sat frozen at the last time it was run.
    #
    # Opened lazily and retried, not once here. DuckDB permits one writer, so a
    # copy of this app still shutting down or a backfill just finishing holds the
    # file for a few seconds - and a desk that tried once at boot answered "the
    # bar store is not open" for the rest of its life, with the file unlocked the
    # whole time. See `marketdata/holder.py`.
    # What each venue may be asked for is what its desk lists. A symbol a source
    # does not list draws nothing rather than something else: the options desk
    # charts the five index underlyings and nothing else. Its constituents are in
    # the store too - the rotation graph reads them - but nothing charts one, so
    # nothing needs them kept current.
    def _register(service: BarService) -> None:
        for spec in listed():
            symbols = listed_on(spec.id)
            if not spec.can(Capability.HISTORY) or not symbols:
                continue
            if not is_configured(spec, settings):
                continue
            service.register(
                spec.id,
                # A way to get an adapter rather than an adapter: the options
                # venue's token expires every morning. See `VenueBars`.
                VenueBars(partial(broker_for, spec)),
                {symbol: symbol for symbol in symbols},
            )
        log.info("bar sources registered: %s", ", ".join(service.sources()) or "none")

    bars = BarStoreHolder(paths.bars_path(), on_open=_register)
    application.state.bars = bars

    perps_venue = serving(AssetClass.PERPETUALS)

    def perps_broker() -> object | None:
        """The perpetuals adapter, or None when that venue is not configured.

        Built per pass rather than held, so a rotated key is picked up, and
        returning None keeps the watcher working on an options-only setup.
        """
        if not is_configured(perps_venue, settings):
            return None
        try:
            return broker_for(perps_venue)
        except Exception:  # noqa: BLE001 - a venue that cannot be built is not watched
            log.warning("could not build the perpetuals adapter for the alert pass")
            return None

    watcher = Watcher(
        gather=lambda: gather(
            db_path, options_broker(), feeds_cache, hub, perps_broker(), codec=codec_for()
        ),
        open_conn=lambda: open_db(db_path),
        notifier=notifier,
    )
    application.state.alert_watcher = watcher
    application.state.alert_notifier = notifier

    task = asyncio.create_task(watcher.run_forever(), name="alert-watcher")
    log.info("alert watcher started (telegram=%s)", notifier is not None)

    # What options cost, written down each session. The one piece of market data
    # on this desk that cannot be fetched again: a price history can be
    # backfilled from any source years later, and what the market was charging
    # for a straddle on a Tuesday afternoon is gone when the session ends. Its
    # own task, because it has to run with the tab closed.
    recorder = VolRecorder(
        underlyings=option_underlyings(),
        fetch_chain=lambda symbol, strikes: options_broker().get_option_chain(
            symbol, strike_count=strikes
        ),
        open_conn=lambda: open_db(db_path),
        in_session=lambda at: in_session(at, get_holidays().dates()),
    )
    application.state.vol_recorder = recorder
    vol_task = asyncio.create_task(recorder.run_forever(), name="vol-recorder")
    log.info("volatility recorder started for %d underlyings", len(option_underlyings()))

    # NSE's pre-open auction, for the same reason: NSE serves the latest session
    # and nothing older. Asks NSE directly, not the broker, so it needs no login.
    preopen = PreOpenRecorder(
        fetch=nse_preopen.fetch,
        open_conn=lambda: open_db(db_path),
        holidays=lambda: get_holidays().dates(),
    )
    application.state.preopen_recorder = preopen
    preopen_task = asyncio.create_task(preopen.run_forever(), name="preopen-recorder")

    # Paper trades on the live chains: stops, targets and expiry, kept by
    # the server so they hold with no page open.
    # Overrides included, as for the options broker above, so a test's fake
    # market is the one the watcher reads.
    paper = PaperWatcher(
        db_path,
        lambda: application.dependency_overrides.get(get_paper_markets, get_paper_markets)(),
        lambda: application.dependency_overrides.get(get_executors, get_executors)(),
    )
    application.state.paper_watcher = paper
    paper_task = asyncio.create_task(paper.run_forever(), name="paper-watcher")

    # The daily bars the rotation graph and the volatility ranks read. Only the
    # backfill script wrote them, and it needs the desk stopped, so they sat at
    # whatever day it was last run. The desk holds the store, so it keeps them.
    def _daily(symbol: str, start: date, end: date) -> list[Bar]:
        return [to_bar(c) for c in options_broker().get_history(symbol, "D", start, end)]

    daily = DailyBarUpdater(
        symbols=lambda: [symbol for _, symbol in daily_series()],
        fetch=_daily,
        store=lambda: bars.store,
        holidays=lambda: get_holidays().dates(),
    )
    application.state.daily_updater = daily
    daily_task = asyncio.create_task(daily.run_forever(), name="daily-bars")

    # The options backtest's history. Only the backfill script wrote it, so the
    # backtest stopped at whichever expiry it was last run after. Daily, out of
    # hours, it fetches what settled since. A venue serving no such history -
    # or a test, where the factory has none - starts nothing.
    backfiller: OptionBackfiller | None = None
    backfill_task: asyncio.Task[None] | None = None
    options_venue = serving(AssetClass.INDEX_OPTIONS)
    expired = expired_source_for(options_venue)
    if expired is not None and is_configured(options_venue, settings):
        backfiller = OptionBackfiller(
            source=expired, store=lambda: OptionStore(paths.options_store_path())
        )
        backfill_task = asyncio.create_task(backfiller.run_forever(), name="option-backfill")
    application.state.option_backfiller = backfiller

    # A venue that pushes prices is streamed rather than polled, which is not a
    # nicety: the perpetuals venue's budget is 60 requests a minute against Fyers'
    # ~200, and three instruments across several panels would spend it on nothing.
    # The hub holds the latest so everything else reads from memory.
    streams: dict[str, AsyncStreaming] = {}
    application.state.tick_hub = hub
    application.state.tick_streams = streams
    for spec in listed():
        stream = stream_for(spec)
        if stream is None or not is_configured(spec, settings):
            continue
        try:
            await stream.start(list(listed_on(spec.id)), hub.publish)
            streams[spec.id] = stream
            log.info("%s tick stream connected", spec.id)
        except Exception:  # noqa: BLE001 - a desk that will not start is worse
            log.warning("%s tick stream could not connect", spec.id, exc_info=True)
            await stream.stop()

    # Option chains that exist only on a venue's socket. Public, so no credential
    # gates it; a feed that will not connect leaves the chain on its stale
    # catalogue prices rather than stopping the desk.
    chain_feeds = []
    for spec in listed():
        chain_feed = chain_feed_for(spec)
        if chain_feed is None:
            continue
        try:
            await chain_feed.start()
            chain_feeds.append(chain_feed)
            log.info("%s chain feed connected", spec.id)
        except Exception:  # noqa: BLE001 - a desk that will not start is worse
            log.warning("%s chain feed could not connect", spec.id, exc_info=True)
            await chain_feed.stop()

    # The account, pushed: an order, a fill or a position change says "read it
    # again", so the portfolio is fetched when it changed rather than every few
    # seconds in case it did. The cached reads are dropped first, so that read
    # goes to the broker.
    account_hub = AccountHub()
    application.state.account_hub = account_hub
    account_stream = (
        account_stream_for(options_venue) if is_configured(options_venue, settings) else None
    )
    if account_stream is not None:

        def on_account(event: AccountEvent) -> None:
            invalidate_account(options_venue)
            account_hub.publish(event)

        try:
            await account_stream.start(on_account)
            log.info("fyers account stream connected")
        except Exception:  # noqa: BLE001 - polling still works without it
            log.warning("fyers account stream could not connect", exc_info=True)
            await account_stream.stop()
            account_stream = None
    application.state.account_stream = account_stream

    try:
        yield
    finally:
        shutting_down.set()
        task.cancel()
        vol_task.cancel()
        preopen_task.cancel()
        paper_task.cancel()
        daily_task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        with suppress(asyncio.CancelledError):
            await vol_task
        with suppress(asyncio.CancelledError):
            await preopen_task
        with suppress(asyncio.CancelledError):
            await paper_task
        with suppress(asyncio.CancelledError):
            await daily_task
        if backfill_task is not None:
            backfill_task.cancel()
            with suppress(asyncio.CancelledError):
                await backfill_task
        # So the pre-open page does not report a recorder that has stopped.
        application.state.preopen_recorder = None
        application.state.daily_updater = None
        for stream in streams.values():
            await stream.stop()
        for chain_feed in chain_feeds:
            await chain_feed.stop()
        if account_stream is not None:
            await account_stream.stop()
        bars.close()
        log.info("alert watcher stopped")


def _set_on_stop_signal(event: asyncio.Event) -> None:
    """Set `event` the moment the server is told to stop, not after it drains.

    The flag above was set in the lifespan's shutdown, but uvicorn only runs that
    after every open response has finished - and a streaming response finishes
    when the flag is set. With the options desk keeping its price stream open
    all day, every reload and every Ctrl-C hung on that circle. So the stop
    signal itself sets the flag, then hands over to uvicorn's own handler.

    Only from the main thread, where signals can be handled at all; a test
    client running the app on a worker thread keeps the old behaviour.
    """
    if threading.current_thread() is not threading.main_thread():
        return
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        previous = signal.getsignal(sig)

        def handler(signum: int, frame: FrameType | None, previous: Any = previous) -> None:
            loop.call_soon_threadsafe(event.set)
            if callable(previous):
                previous(signum, frame)

        signal.signal(sig, handler)


app = FastAPI(title="Option Strategy Dashboard API", lifespan=lifespan)

# Local dev only: the Vite dev server runs on a different port than uvicorn.
# Tighten this (or drop it behind a reverse proxy) before exposing this
# beyond localhost - see docs/SETUP.md.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
    # PUT included because the alert thresholds are edited with one. Its absence
    # was invisible from the server's side - the endpoint worked, and every
    # request from the browser died in the preflight instead, which reaches the
    # page as "Failed to fetch" with nothing in the server log.
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)

# Broker failures become statuses a client can act on, with a stable `code`,
# instead of an opaque 500 that leaves the desk frozen with no explanation.
app.add_exception_handler(BrokerError, broker_error_handler)

# One router per area of the desk. Order is presentational only - every path is
# distinct - except that all of them must be registered before the mount below.
for _router in (
    system.router,
    alerts.router,
    backtest.router,
    optbt.router,
    simulator.router,
    live.router,
    strategies.router,
    preopen.router,
    bars.router,
    chart.router,
    portfolio.router,
    market.router,
    perps.router,
    crypto_options.router,
    rrg.router,
    feeds.router,
    baskets.router,
    structure.router,
    volatility.router,
):
    app.include_router(_router)

# ---------------------------------------------------------------------------
# The built frontend, when there is one.
#
# Mounted last so every /api route above is matched first; a mount at "/" would
# otherwise swallow them. Absent in development, where Vite serves the frontend
# on its own port and CORS above lets it through - so this is a no-op then, and
# the two setups need no switch between them.
_UI_DIR = Path(__file__).resolve().parent.parent / "frontend" / "dist"


class SinglePage(StaticFiles):
    """Static files, with the app's own index.html for any path that is not a file.

    Needed because `html=True` alone does not do this: it serves index.html for a
    directory and 404s for everything else, so /options and /crypto answered 404 on
    a reload even though the app knows both routes. A single-page app has no files
    at its routes by definition, so the fallback has to be here.

    Only for reads: a POST to a path that does not exist is a mistake worth
    reporting, not a page to render.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as missing:
            wants_a_file = "." in path.rsplit("/", 1)[-1]
            if missing.status_code != 404 or wants_a_file:
                # A missing script or stylesheet is a broken build, and answering
                # it with a page of HTML turns that into a baffling parse error
                # in the console instead of an honest 404.
                raise
            return await super().get_response("index.html", scope)


if _UI_DIR.is_dir():
    # Mounted last so every /api route above is matched first; a mount at "/"
    # would otherwise swallow them.
    app.mount("/", SinglePage(directory=_UI_DIR, html=True), name="ui")
