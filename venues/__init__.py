"""What the desk can trade, and where.

A venue is one place with one account, one set of instruments and one trading
calendar. The desk started with exactly one, so "the broker" and "the market"
were the same thing everywhere; this package is where that assumption is
undone, before a second venue makes it expensive.

Deliberately free of credentials and adapter construction: this package only
describes venues. Building a broker for one is `broker/factory.py`'s job, so
that importing the catalogue never needs a key and never touches the network.
"""

from venues.calendar import Session, is_open
from venues.instruments import (
    OPTION_UNDERLYINGS,
    Instrument,
    for_venue,
    instrument,
    listed_on,
    option_underlyings,
)
from venues.models import AssetClass, Capability, VenueSpec
from venues.registry import FYERS, SHARK, SHARK_OPTIONS, VENUES, get, listed, serving

__all__ = [
    "FYERS",
    "SHARK",
    "SHARK_OPTIONS",
    "Instrument",
    "VENUES",
    "AssetClass",
    "Capability",
    "Session",
    "VenueSpec",
    "get",
    "OPTION_UNDERLYINGS",
    "for_venue",
    "option_underlyings",
    "instrument",
    "listed_on",
    "is_open",
    "listed",
    "serving",
]
