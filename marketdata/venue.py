"""A broker's own history, as a bar source.

The bars you trade from. A venue serves a short window - Shark gives a few hundred
candles - so its answers are stored as they arrive and the history accumulates from
the venue itself: a few hundred today, a few hundred overlapping tomorrow, and after
a month a month of the exact instrument the position is in.

That is the whole fix for a short history, and it needs no other source. Yahoo and
Binance are for context and for seeding what does not exist yet, and neither quotes
what Shark quotes - its gold is a perpetual where Yahoo's is a dated contract 0.8%
away, and its Bitcoin carries funding where Binance's is spot.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

from broker.base import MarketData
from broker.errors import BrokerError
from broker.errors import RateLimited as BrokerRateLimited
from broker.models import Candle
from marketdata.errors import RateLimited, Unavailable
from marketdata.models import Bar, Fetched, Interval
from venues.calendar import IST

log = logging.getLogger(__name__)

#: How the desk's intervals map onto the resolution strings a broker wants. The
#: options broker counts minutes; this is the same table the perps router uses, held
#: here so a source can be built without one.
RESOLUTION: dict[Interval, str] = {
    Interval.M1: "1",
    Interval.M5: "5",
    Interval.M15: "15",
    Interval.M30: "30",
    Interval.H1: "60",
    Interval.H4: "240",
    Interval.D1: "D",
    Interval.W1: "W",
}



def to_bar(candle: Candle) -> Bar:
    """A broker's candle as a stored bar. A naive timestamp is read as UTC.

    The one place the two shapes meet, so every path from a broker into the
    store - the chart's refresh, the daily updater, the backfill - agrees.
    """
    ts = candle.timestamp if candle.timestamp.tzinfo else candle.timestamp.replace(tzinfo=UTC)
    return Bar(
        ts=ts,
        open=candle.open,
        high=candle.high,
        low=candle.low,
        close=candle.close,
        volume=candle.volume,
    )

def weekday_bars(bars: list[Bar]) -> list[Bar]:
    """Bars whose IST day is a weekday.

    For the live fetch only. NSE runs mock trading sessions on Saturdays and
    Fyers' live candle carries their ticks as if they traded: on 10 Oct 2026 that
    was a NIFTY daily bar 2,200 points above the days either side of it. Its
    history endpoint does not serve mock sessions but does serve the real weekend
    ones - Budget days, Muhurat, the 2024 special sessions - so the daily updater
    and the backfill keep weekends, and those fill a real one in after the fact.
    """
    return [b for b in bars if b.ts.astimezone(IST).weekday() < 5]


class VenueBars:
    """Bars from a broker adapter, for storing under that venue's name."""

    def __init__(self, broker: Callable[[], MarketData], *, weekdays_only: bool = False) -> None:
        #: How to get an adapter, rather than one adapter. The options venue's
        #: access token expires every morning and is replaced by whoever asks for
        #: it next, so a source that held one adapter for the life of the process
        #: would be fetching with a dead token by the second day - and a chart
        #: that stops updating overnight is the bug this whole path exists to fix.
        self._broker = broker
        #: For an NSE venue. See `weekday_bars`.
        self._weekdays_only = weekdays_only

    def fetch(self, symbol: str, interval: Interval, days: int) -> Fetched:
        """Whatever the venue will give for the last `days`.

        A venue's own rate limit is the reason this is worth storing: Shark allows 60
        requests a minute across everything, and a chart redrawn on every timeframe
        change would spend that on candles already on disk.
        """
        today = datetime.now(UTC).date()
        start: date = today - timedelta(days=max(1, days))
        try:
            broker = self._broker()
        except Exception as exc:  # noqa: BLE001 - an adapter that cannot be built has no bars
            # A missing or unrefreshable credential arrives here. Reported rather
            # than raised, like a refusal: the store still has yesterday, and a
            # chart with a note saying why it stopped is worth more than a 500.
            raise Unavailable(f"the {type(exc).__name__} was: {exc}") from exc
        try:
            candles = broker.get_history(symbol, RESOLUTION[interval], start, today)
        except BrokerRateLimited as exc:
            raise RateLimited(str(exc.message)) from exc
        except BrokerError as exc:
            raise Unavailable(str(exc.message)) from exc

        bars = [to_bar(c) for c in candles]
        bars.sort(key=lambda b: b.ts)
        if self._weekdays_only:
            bars = weekday_bars(bars)
        return Fetched(bars=bars, name=symbol, currency="")
