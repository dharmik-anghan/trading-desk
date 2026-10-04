"""The catalogue of venues the desk knows about.

One entry per venue. Adding a venue is adding an entry here plus an adapter in
`broker/` and a factory line in `broker/factory.py` - nothing above this
package needs to change, which is the whole point of it existing.
"""

from __future__ import annotations

from venues.calendar import Session
from venues.models import AssetClass, Capability, VenueSpec

FYERS = VenueSpec(
    id="fyers",
    name="NSE index options",
    asset_class=AssetClass.INDEX_OPTIONS,
    quote_currency="INR",
    session=Session.NSE_FO,
    capabilities=frozenset(
        {
            Capability.QUOTES,
            Capability.HISTORY,
            Capability.TRADING,
            Capability.FUNDS,
            Capability.OPTION_CHAIN,
            # Declared because the protocol is implemented. It raises
            # NotImplementedError today - see broker/fyers.py - so a client
            # should treat this as "the adapter has the method", not "ticks
            # work". When streaming lands this comment goes away rather than
            # the capability appearing.
            Capability.STREAMING,
        }
    ),
)

SHARK = VenueSpec(
    id="shark",
    name="Crypto & commodities",
    asset_class=AssetClass.PERPETUALS,
    # Prices and charts in USDT, money in INR. Not a simplification: a closed
    # XAUUSDT position reports marginAsset INR and a marginConversionRate beside
    # the USDT figure, so the two really are in different units.
    quote_currency="USDT",
    margin_currency="INR",
    # The venue never closes; gold and oil do. That is per instrument, in
    # venues/instruments.py, because they sit on this same venue.
    session=Session.ALWAYS,
    capabilities=frozenset(
        {
            Capability.QUOTES,
            Capability.HISTORY,
            Capability.TRADING,
            Capability.PERPETUALS,
            # No FUNDS: the documented wallet endpoint answers 404, so this
            # adapter cannot say what the account holds. No OPTION_CHAIN either -
            # these are perpetuals and never expire. STREAMING lands with the
            # socket.io client; declaring it before then would promise a panel
            # that cannot be filled.
        }
    ),
)

SHARK_OPTIONS = VenueSpec(
    id="shark_options",
    name="Crypto options",
    asset_class=AssetClass.CRYPTO_OPTIONS,
    # Premiums and strikes in USDT, settled to an INR account - the same split as
    # the perpetuals, on a separate service.
    quote_currency="USDT",
    margin_currency="INR",
    session=Session.ALWAYS,
    capabilities=frozenset(
        {
            Capability.QUOTES,
            # The underlying's candles, from its perpetual. Option contracts have
            # none: the venue serves no option history at all.
            Capability.HISTORY,
            Capability.OPTION_CHAIN,
            # No TRADING or FUNDS yet: orders and the options wallet need a key
            # and an options account, and come with live execution.
        }
    ),
)

#: Insertion order is the order the switcher shows them in.
VENUES: dict[str, VenueSpec] = {FYERS.id: FYERS, SHARK.id: SHARK, SHARK_OPTIONS.id: SHARK_OPTIONS}

#: The venue used when a request does not name one. Every endpoint that existed
#: before venues did keeps working unchanged because of this.
DEFAULT_VENUE_ID = FYERS.id


class UnknownVenueError(KeyError):
    """Asked for a venue that is not in the catalogue."""


def get(venue_id: str | None = None) -> VenueSpec:
    """Look up a venue, falling back to the default when not named."""
    wanted = venue_id or DEFAULT_VENUE_ID
    try:
        return VENUES[wanted]
    except KeyError:
        known = ", ".join(VENUES)
        raise UnknownVenueError(f"unknown venue {wanted!r}; known venues: {known}") from None


def listed() -> list[VenueSpec]:
    return list(VENUES.values())


def serving(asset_class: AssetClass) -> VenueSpec:
    """The venue a desk for this asset class trades on: the first one listed.

    One per desk today. A second options broker becomes a choice the desk
    offers, and this becomes its default.
    """
    for spec in VENUES.values():
        if spec.asset_class is asset_class:
            return spec
    raise UnknownVenueError(f"no venue lists {asset_class.value}")
