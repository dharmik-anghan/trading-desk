"""FastAPI dependencies: the providers, and the typed annotations routers use.

Held apart from `api/app.py` so a router can import them without importing the
application - which would be a cycle, since `app.py` imports the routers.

Providers are thin functions rather than values built at import, so tests can
override them via `app.dependency_overrides` with a `FakeBroker` or a temporary
database, without real credentials or the real `data/trading.db`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import Depends, HTTPException, Request

import paths
from broker.base import Broker, OptionsBroker
from broker.contracts import ContractCodec
from broker.factory import (
    broker_for,
    codec_for,
    is_configured,
    option_chains_for,
    options_broker,
)
from broker.shark.options import SharkOptionsBroker
from broker.shark.options_account import SharkOptionsAccount
from feeds.fetch import Feeds
from feeds.holidays import Holidays
from marketdata import BarService, BarStore
from marketdata.holder import BarStoreHolder
from paper.live import Limits, SharkExecutor
from paper.markets import Executor, NseMarket, PaperMarket, SharkMarket
from settings import load_settings
from venues import SHARK_OPTIONS, AssetClass, VenueSpec, serving
from venues import get as get_venue
from venues.registry import UnknownVenueError


def options_venue(venue: str = "") -> VenueSpec:
    """Which options venue a request is for: `?venue=` if given, else the default.

    Every options endpoint takes it, so a second options broker is served by the
    same routes, panels and analytics as the first - only the adapter differs.
    """
    try:
        spec = get_venue(venue) if venue else serving(AssetClass.INDEX_OPTIONS)
    except UnknownVenueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    if spec.asset_class is not AssetClass.INDEX_OPTIONS:
        raise HTTPException(status_code=400, detail=f"{spec.id} does not list options")
    return spec


OptionsVenueDep = Annotated[VenueSpec, Depends(options_venue)]


def get_broker(venue: OptionsVenueDep) -> OptionsBroker:
    """The options broker for this request's venue. See `broker/factory.py`."""
    return options_broker(venue)


def perps_venue() -> VenueSpec:
    """The venue the perpetuals desk trades on."""
    return serving(AssetClass.PERPETUALS)


PerpsVenueDep = Annotated[VenueSpec, Depends(perps_venue)]


def get_perps_broker(venue: PerpsVenueDep) -> Broker:
    """The perpetuals desk's broker. See `broker/factory.py`."""
    return broker_for(venue)


def get_crypto_options() -> SharkOptionsBroker:
    """The crypto options desk's adapter. Public data, so no credential to check.

    The concrete adapter rather than a protocol: the desk reads the venue's fee
    terms and order books, which no other options venue has a shape for yet.
    """
    adapter = option_chains_for(serving(AssetClass.CRYPTO_OPTIONS))
    if not isinstance(adapter, SharkOptionsBroker):
        raise HTTPException(status_code=500, detail="crypto options venue is not Shark")
    return adapter


_nse_market: NseMarket | None = None


def get_paper_markets() -> dict[str, PaperMarket]:
    """The live markets paper trades can be placed on, by venue id.

    Shark's is public and always there. The NSE's needs Fyers, so it is left out
    while Fyers is not configured rather than offered and then refused. Built
    once: it remembers lot sizes and contracts as chains are read.
    """
    global _nse_market
    out: dict[str, PaperMarket] = {SHARK_OPTIONS.id: SharkMarket(get_crypto_options())}
    nse = serving(AssetClass.INDEX_OPTIONS)
    if is_configured(nse):
        broker = options_broker(nse)
        if _nse_market is None:
            _nse_market = NseMarket(broker, codec_for(nse), broker, holidays=_holidays.dates)
        out[nse.id] = _nse_market
    return out


def get_shark_options_account() -> SharkOptionsAccount | None:
    """The Shark options account, when its key is set. Reading it is harmless;
    trading through it needs `get_executors` as well."""
    settings = load_settings()
    if not settings.has_shark:
        return None
    return SharkOptionsAccount(settings.shark_api_key, settings.shark_api_secret)


def get_executors() -> dict[str, Executor]:
    """What can send real orders, by venue. Empty unless live trading is on.

    Only Shark's options for now; the NSE's comes when Fyers orders are wired.
    """
    settings = load_settings()
    account = get_shark_options_account()
    if not settings.shark_options_live or account is None:
        return {}
    return {SHARK_OPTIONS.id: SharkExecutor(account, SharkMarket(get_crypto_options()))}


def get_live_limits() -> Limits:
    settings = load_settings()
    return Limits(
        enabled=settings.shark_options_live,
        max_notional=settings.shark_max_notional,
        daily_loss=settings.shark_options_daily_loss,
    )


def get_codec(venue: OptionsVenueDep) -> ContractCodec:
    """How this request's venue spells its contracts. See `broker/contracts.py`."""
    return codec_for(venue)


def get_db_path() -> Path:
    """Where the database lives. Honours `DB_PATH`, like every script does."""
    return paths.db_path()


# One set of feeds for the process, so the calendar is fetched a few times a
# day rather than once per request. Holds its own cache - see feeds/fetch.py.
_feeds = Feeds()


def get_feeds() -> Feeds:
    return _feeds


# The exchange's holiday list, fetched once a day. Shared so the session check
# does not go to the network on every request.
_holidays = Holidays()


def get_holidays() -> Holidays:
    return _holidays


BrokerDep = Annotated[OptionsBroker, Depends(get_broker)]
CodecDep = Annotated[ContractCodec, Depends(get_codec)]
PerpsBrokerDep = Annotated[Broker, Depends(get_perps_broker)]
PaperMarketsDep = Annotated[dict[str, PaperMarket], Depends(get_paper_markets)]
ExecutorsDep = Annotated[dict[str, Executor], Depends(get_executors)]
LiveLimitsDep = Annotated[Limits, Depends(get_live_limits)]
SharkAccountDep = Annotated[SharkOptionsAccount | None, Depends(get_shark_options_account)]
CryptoOptionsDep = Annotated[SharkOptionsBroker, Depends(get_crypto_options)]
FeedsDep = Annotated[Feeds, Depends(get_feeds)]
HolidaysDep = Annotated[Holidays, Depends(get_holidays)]
DbPathDep = Annotated[Path, Depends(get_db_path)]


def bar_service(request: Request) -> BarService | None:
    """The bar store's service, or None while the file is held elsewhere.

    One place, because four routers wanted it and each had grown its own
    `getattr` against app state. The holder retries on demand - see
    `marketdata/holder.py` - so a desk that started beside a finishing backfill
    recovers on its own rather than refusing history for the rest of the day.

    A service wired directly onto app state wins over the holder. That is how
    the tests supply one, and an explicit override losing to the real thing is
    the wrong way round - it would have them reading whatever is on this
    machine's disk.
    """
    direct = getattr(request.app.state, "bar_service", None)
    if isinstance(direct, BarService):
        return direct
    holder = getattr(request.app.state, "bars", None)
    return holder.service() if isinstance(holder, BarStoreHolder) else None


def require_bar_service(request: Request, cannot: str) -> BarService:
    """The bar store's service, or a 503 saying what `cannot` be done without it.

    Opened on demand and retried, not once at startup: a desk that came up
    beside a finishing backfill used to answer this for the rest of the day
    with the file unlocked the whole time.
    """
    service = bar_service(request)
    if service is None:
        raise HTTPException(
            status_code=503,
            detail=(
                f"The bar store is open in another process, so {cannot}. "
                "It is retried every 30 seconds - a backfill script or a second "
                "copy of the app will be holding it."
            ),
        )
    return service


def bar_store(request: Request) -> BarStore | None:
    """The store itself, for the few reads that are not bars - funding rates."""
    direct = getattr(request.app.state, "bar_store", None)
    if isinstance(direct, BarStore):
        return direct
    holder = getattr(request.app.state, "bars", None)
    return holder.store if isinstance(holder, BarStoreHolder) else None
