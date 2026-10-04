"""What one venue is."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from venues.calendar import Session


class AssetClass(StrEnum):
    """What kind of thing a venue lists.

    This drives which panels a desk shows, because the two share almost no
    vocabulary: index options have strikes, expiries, greeks and a payoff at
    expiry, while perpetuals have leverage, funding and a liquidation price and
    never expire. Kept as the asset class rather than the venue id so a second
    options broker or a second perps venue slots in without new branches.
    """

    INDEX_OPTIONS = "index_options"
    PERPETUALS = "perpetuals"
    #: Options on a crypto underlying: strikes and expiries like index options,
    #: but a market that never closes, quantities in fractions of a coin, and
    #: prices in USDT with the account in INR. Its own class so the NSE desk's
    #: panels, which assume a session and lot sizes, are not offered it.
    CRYPTO_OPTIONS = "crypto_options"


class Capability(StrEnum):
    """One thing a venue's adapter can do.

    Mirrors the protocols in `broker/base.py`. Declared on the venue so the API
    can tell a client what to show without constructing an adapter (which needs
    credentials); `tests/venues/test_registry.py` asserts the declaration
    matches what the adapter actually implements, so it cannot quietly rot.
    """

    QUOTES = "quotes"
    HISTORY = "history"
    #: Positions, and placing orders against them.
    TRADING = "trading"
    #: Reading account balances. Held apart from TRADING because a venue can
    #: allow the one without exposing the other - which decides whether a
    #: pre-trade margin check is possible at all.
    FUNDS = "funds"
    OPTION_CHAIN = "option_chain"
    #: Leveraged positions: margin, leverage and a liquidation price. What a perps
    #: desk needs and an options desk has no use for.
    PERPETUALS = "perpetuals"
    STREAMING = "streaming"


@dataclass(frozen=True)
class VenueSpec:
    """A venue, described. No connection, no credentials, no I/O."""

    id: str
    #: Shown in the venue switcher.
    name: str
    asset_class: AssetClass
    #: What prices are quoted in - what a chart's axis is measured in.
    quote_currency: str
    #: When it trades. A venue default; an instrument can differ from it, which
    #: is what venues/instruments.py is for.
    session: Session
    capabilities: frozenset[Capability]
    #: What the account is denominated in, when that is not the quote currency.
    #:
    #: They differ on Shark: contracts are quoted in USDT while the account
    #: margins in INR, and a position reports its P&L in both with the conversion
    #: rate it used. Carrying one currency for the pair would mean labelling one
    #: of those figures wrongly, and a mislabelled number is worse than an
    #: unfamiliar one.
    margin_currency: str = ""

    @property
    def money_currency(self) -> str:
        """What a balance or a P&L figure is in - the quote currency, unless the
        venue settles in something else."""
        return self.margin_currency or self.quote_currency

    def can(self, capability: Capability) -> bool:
        return capability in self.capabilities
