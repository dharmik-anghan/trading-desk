"""What a day looked like before a decision: pivots, gap, VIX, days to expiry.

Everything here is known *before* the entry it describes. Pivots come from the
previous session's high, low and close. The gap is today's open against
yesterday's close. The VIX percentile ranks the VIX at the decision against
earlier days' closes only - including today's close would be ranking a number
against a day that has not finished.

Used twice, and it is the same definition both times: as a day filter ("only
trade when the VIX percentile is above 70") and as a tag on every trade, so a
result can be sliced by any of these afterwards.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import date, datetime

from optbt.source import MarketSource
from venues.instruments import INDIA_VIX

#: Where the open sits against the day's pivots. Ordered low to high.
ZONES = ("below S2", "S2-S1", "S1-P", "P-R1", "R1-R2", "above R2")


@dataclass(frozen=True)
class Pivots:
    """Classic floor pivots from one session's high, low and close."""

    p: float
    r1: float
    r2: float
    r3: float
    s1: float
    s2: float
    s3: float

    @classmethod
    def of(cls, high: float, low: float, close: float) -> Pivots:
        p = (high + low + close) / 3
        return cls(
            p=p,
            r1=2 * p - low,
            r2=p + (high - low),
            r3=high + 2 * (p - low),
            s1=2 * p - high,
            s2=p - (high - low),
            s3=low - 2 * (high - p),
        )

    def zone(self, price: float) -> str:
        edges = (self.s2, self.s1, self.p, self.r1, self.r2)
        return ZONES[bisect.bisect_right(edges, price)]

    def level(self, name: str) -> float:
        return float(getattr(self, name.lower()))


@dataclass(frozen=True)
class Day:
    day: date
    open: float
    high: float
    low: float
    close: float
    prev_close: float | None
    prev_high: float | None
    prev_low: float | None
    #: From the previous session. None on the first day of the data.
    pivots: Pivots | None
    vix_close: float | None

    @property
    def gap_pct(self) -> float | None:
        if self.prev_close is None:
            return None
        return (self.open - self.prev_close) / self.prev_close * 100

    @property
    def open_zone(self) -> str | None:
        return self.pivots.zone(self.open) if self.pivots else None


class Context:
    """Daily context for one underlying, loaded once per run."""

    def __init__(self, source: MarketSource) -> None:
        self._source = source
        vix = {d: c for d, _o, _h, _lo, c in source.daily(INDIA_VIX)}
        self._days: dict[date, Day] = {}
        prev: tuple[float, float, float] | None = None
        for d, o, h, lo, c in source.daily(source.index_symbol):
            self._days[d] = Day(
                day=d,
                open=o,
                high=h,
                low=lo,
                close=c,
                prev_close=prev[2] if prev else None,
                prev_high=prev[0] if prev else None,
                prev_low=prev[1] if prev else None,
                pivots=Pivots.of(*prev) if prev else None,
                vix_close=vix.get(d),
            )
            prev = (h, lo, c)
        self._order = sorted(self._days)
        self._vix_closes = [self._days[d].vix_close for d in self._order]

    def day(self, d: date) -> Day | None:
        return self._days.get(d)

    def vix_at(self, ts: datetime) -> float | None:
        """India VIX at the close of the bar named `ts`, or the last one before."""
        return self._source.close_at(INDIA_VIX, ts)

    def vix_percentile(self, d: date, value: float, lookback: int = 252) -> float | None:
        """Where `value` ranks among the `lookback` sessions' VIX closes before `d`.

        0 is below every one of them, 100 above every one. None until there are
        at least a quarter of `lookback` earlier sessions to rank against.
        """
        i = bisect.bisect_left(self._order, d)
        earlier = [v for v in self._vix_closes[max(0, i - lookback) : i] if v is not None]
        if len(earlier) < max(20, lookback // 4):
            return None
        earlier.sort()
        return bisect.bisect_left(earlier, value) / len(earlier) * 100
