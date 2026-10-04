"""Where option history comes from: what a source must answer.

The backfill asks for expiries, the contracts listed on one, and their candles.
An adapter answers - Fyers' expired F&O client is `optbt/data/fyers.py`;
tests use a fake.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol

from optbt.data.models import Candle, Contract, Expiries

#: Longest span one intraday request may cover. Fyers' limit, and the page the
#: backfill asks for, so no source is asked for more than it will give.
MAX_SPAN_DAYS = 100


class SourceError(RuntimeError):
    """The source answered, and the answer was not data."""


class ExpiredSource(Protocol):
    """What the backfill needs from a source."""

    def expiries(self, underlying: str, start: date, end: date) -> Expiries: ...

    def contracts(self, underlying: str, expiry: date) -> list[Contract]: ...

    def candles(self, symbol: str, start: date, end: date) -> list[Candle]: ...

    def index_candles(
        self, symbol: str, resolution: str, start: date, end: date
    ) -> list[Candle]: ...


class LiveSource(ExpiredSource, Protocol):
    """A source that also serves contracts still trading - up to yesterday."""

    def live_expiries(self, underlying: str) -> list[tuple[date, bool]]:
        """Expiries still trading, nearest first, with whether each is a monthly."""
        ...

    #: Seconds between requests; settable, to share a rate limit.
    interval: float
