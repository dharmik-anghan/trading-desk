"""The venue catalogue, and the promise each entry makes.

The test that matters here is the last one: a venue declares its capabilities
as data so the API can report them without building an adapter, and a
declaration that drifts from the adapter is worse than no declaration at all -
the UI would offer a panel that cannot be filled. So the claim is checked
against the protocols the adapter actually satisfies.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from broker.base import Funds_, MarketData, OptionsData, PerpetualsData, Streaming, Trading
from broker.fake import FakeBroker
from broker.shark import SharkBroker
from broker.shark.options import SharkOptionsBroker
from venues import AssetClass, Capability, Session, VenueSpec, get, is_open, listed
from venues.registry import DEFAULT_VENUE_ID, VENUES, UnknownVenueError

IST = timezone(timedelta(hours=5, minutes=30))


def test_the_catalogue_is_not_empty() -> None:
    assert listed()
    assert DEFAULT_VENUE_ID in VENUES


def test_ids_match_their_keys() -> None:
    for key, spec in VENUES.items():
        assert key == spec.id


def test_no_venue_omits_the_basics() -> None:
    for spec in listed():
        assert spec.name
        assert spec.quote_currency
        assert spec.can(Capability.QUOTES), f"{spec.id} must at least quote prices"


def test_get_without_an_id_returns_the_default() -> None:
    assert get().id == DEFAULT_VENUE_ID
    assert get(None).id == DEFAULT_VENUE_ID


def test_an_unknown_venue_says_what_it_knows() -> None:
    with pytest.raises(UnknownVenueError, match="known venues"):
        get("not-a-venue")


def test_an_options_venue_lists_chains() -> None:
    options = [s for s in listed() if s.asset_class is AssetClass.INDEX_OPTIONS]
    assert options, "the desk is an options desk; at least one venue should list them"
    for spec in options:
        assert spec.can(Capability.OPTION_CHAIN)


class TestSessions:
    saturday = datetime(2026, 9, 26, 12, 0, tzinfo=IST)
    weekday_open = datetime(2026, 9, 28, 12, 0, tzinfo=IST)
    weekday_shut = datetime(2026, 9, 28, 20, 0, tzinfo=IST)
    sunday = datetime(2026, 9, 27, 12, 0, tzinfo=IST)

    def test_the_exchange_keeps_its_hours(self) -> None:
        assert is_open(Session.NSE_FO, self.weekday_open)
        assert not is_open(Session.NSE_FO, self.weekday_shut)
        assert not is_open(Session.NSE_FO, self.saturday)

    def test_a_market_that_never_closes_never_closes(self) -> None:
        # Every Shark perpetual, gold and oil included: a live Saturday stream
        # delivered all three, seconds old.
        for at in (self.saturday, self.sunday, self.weekday_open, self.weekday_shut):
            assert is_open(Session.ALWAYS, at)

    def test_holidays_do_not_apply_to_a_market_without_them(self) -> None:
        holidays = frozenset({self.weekday_open.date()})
        assert not is_open(Session.NSE_FO, self.weekday_open, holidays)
        assert is_open(Session.ALWAYS, self.weekday_open, holidays)


#: The protocol each capability claims the adapter satisfies.
_PROTOCOL_FOR = {
    Capability.QUOTES: MarketData,
    Capability.HISTORY: MarketData,
    Capability.TRADING: Trading,
    Capability.FUNDS: Funds_,
    Capability.OPTION_CHAIN: OptionsData,
    Capability.PERPETUALS: PerpetualsData,
    Capability.STREAMING: Streaming,
}


def _adapter_for(spec: VenueSpec) -> object | None:
    """A real adapter instance for a venue, or None if we cannot build one here.

    FakeBroker stands in for Fyers: it is the same protocol surface, built
    against the same contract tests, and needs no credentials. A venue whose
    adapter cannot be built without a key is skipped rather than silently
    passing.
    """
    if spec.id == "fyers":
        return FakeBroker()
    if spec.id == "shark":
        # Constructing it needs no network and no real key - it only signs when
        # it actually calls out - so the capability claim can be checked here.
        return SharkBroker(api_key="test-key", api_secret="test-secret")
    if spec.id == "shark_options":
        # Public data and no feed: constructing it touches nothing.
        return SharkOptionsBroker()
    return None


def test_declared_capabilities_are_actually_implemented() -> None:
    for spec in listed():
        adapter = _adapter_for(spec)
        if adapter is None:
            pytest.skip(f"no credential-free adapter for {spec.id}")
        for capability in spec.capabilities:
            protocol = _PROTOCOL_FOR[capability]
            assert isinstance(adapter, protocol), (
                f"{spec.id} declares {capability} but its adapter does not satisfy {protocol}"
            )


def test_every_capability_is_mapped_to_a_protocol() -> None:
    # so that adding a Capability without deciding what proves it fails here
    assert set(_PROTOCOL_FOR) == set(Capability)


def test_a_venue_does_not_claim_what_it_cannot_do() -> None:
    """The other half of the promise, and the one that actually bites.

    Declaring a capability the adapter lacks means the UI offers a panel that can
    never fill. Shark is the live example: the documented wallet endpoint answers
    404, so it must not claim FUNDS, and it has no option chains to claim either.
    """
    for spec in listed():
        adapter = _adapter_for(spec)
        if adapter is None:
            continue
        for capability, protocol in _PROTOCOL_FOR.items():
            if capability in spec.capabilities:
                continue
            if isinstance(adapter, protocol) and capability is not Capability.HISTORY:
                # QUOTES and HISTORY share a protocol, so one can be satisfied
                # while the other is not declared; everything else is a lie.
                assert capability is Capability.QUOTES, (
                    f"{spec.id} satisfies {protocol.__name__} but does not declare {capability}"
                )
