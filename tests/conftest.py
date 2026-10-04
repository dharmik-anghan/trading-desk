"""Guards that apply to every test.

One job: no test may reach a real venue. This is not belt-and-braces - it already
happened. When dry-run was removed from the order path, a test posting a sound
order built a real Shark adapter from the credentials in `.env` and attempted to
place a live order. The venue refused it for an unrelated reason, so nothing was
opened, and the only thing standing between that test and a real position was
luck.

The API tests override `get_broker`, which covers the options desk. They do not
override `broker_for`, which is how the perpetuals endpoints resolve a venue - and
that is the gap. Rather than ask every test to remember, the adapter factory is
replaced here for the whole suite, so reaching the venue is impossible by
construction and a test that needs a broker has to say so.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import replace

import pytest

from broker.base import Broker


class VenueReachedInTest(AssertionError):
    """A test tried to build a live venue adapter."""


@pytest.fixture(autouse=True)
def _no_live_venues(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Replace every real adapter factory with one that refuses.

    A test wanting broker behaviour stubs `broker_for` or `get_broker` itself,
    which is explicit and local. A test that forgets gets a loud failure naming
    this file rather than a silent request to a live account.
    """
    import broker.factory as factory

    def refuse(name: str) -> Callable[[], Broker]:
        def build() -> Broker:
            raise VenueReachedInTest(
                f"A test tried to build the live {name} adapter. Stub broker_for or "
                f"get_broker in the test instead - see tests/conftest.py."
            )

        return build

    import broker.shark.options_account as options_account

    def no_account(*args: object, **kwargs: object) -> object:
        raise VenueReachedInTest(
            "A test tried to reach the live Shark options account. Override "
            "get_shark_options_account and get_executors instead - see tests/conftest.py."
        )

    # The options account is built from the settings by its own dependency, not
    # through the factory, so it is refused here separately: a live order in a
    # test must go through a fake executor or not at all.
    monkeypatch.setattr(options_account.SharkOptionsAccount, "_request", no_account)

    for venue_id, real in list(factory.FACTORIES.items()):
        monkeypatch.setitem(
            factory.FACTORIES,
            venue_id,
            replace(
                real,
                build=refuse(venue_id),
                stream=None,
                account=None,
                expired=None,
                chain_feed=None,
            ),
        )
    yield


@pytest.fixture(autouse=True)
def _no_daily_bar_pass(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The app's daily-bar updater, idle.

    Entering `TestClient` starts it, and it walks a few hundred symbols with a
    pause between each. Against the refusing factories above every one fails,
    so an app start in a test cost over a minute of pauses. Its own tests call
    `tick()` directly.
    """
    import jobs.daily_bars as daily_updater

    async def idle(self: object) -> None:
        return None

    monkeypatch.setattr(daily_updater.DailyBarUpdater, "run_forever", idle)
    yield


class NseReachedInTest(AssertionError):
    """A test tried to fetch from NSE."""


@pytest.fixture(autouse=True)
def _no_nse(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """NSE's pre-open, refused, and the app's own database moved aside.

    The same accident as above in a quieter form: a test entering `TestClient`
    runs the app's lifespan, which starts the pre-open recorder, which fetched
    NSE for real and wrote the answer into `data/trading.db`. The dependency
    override on `get_db_path` does not reach the lifespan - it calls the function
    directly - so `DB_PATH` points it at a scratch file instead.
    """
    import tempfile

    import marketdata.nse_preopen as nse_preopen

    def refuse(*args: object, **kwargs: object) -> object:
        raise NseReachedInTest("A test tried to fetch from NSE. Stub nse_preopen.fetch.")

    monkeypatch.setattr(nse_preopen, "fetch", refuse)
    with tempfile.TemporaryDirectory() as scratch:
        monkeypatch.setenv("DB_PATH", f"{scratch}/trading.db")
        yield
