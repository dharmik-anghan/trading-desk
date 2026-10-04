"""The market's shapes, and what a strategy is allowed to see of it.

Two readers, deliberately separate:

  A `MarketSource` (`optbt/source.py`; the store's is `optbt/data/history.py`)
  is what the engine fills orders from, which means it reads the bar a fill
  happens in - a bar the strategy has not seen close.

  `View` is what a strategy decides with. It is pinned to the last *closed* bar
  and has no way to read past it. Every "beautiful meaningless equity curve" in
  options backtesting comes from a strategy that could see a price from the
  minute it was trading in; making the strategy's only window unable to do that
  is cheaper than checking every strategy for it.

A bar is named by the minute it starts. The bar named 09:19 closes at 09:20, so a
decision "at 09:20" is made on the 09:19 close and fills in the 09:20 bar.

The session is the index's: 09:15 to 15:29. Option bars run to 15:39 in the store
- a closing session with real volume - but nothing can be opened or closed there
on a normal order, so the engine never looks at them.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING

from broker.models import OptionType
from optbt.data.models import Kind
from optbt.marks import reprice, years_to
from venues.calendar import NSE_OPEN

if TYPE_CHECKING:
    from optbt.context import Context
    from optbt.source import MarketSource

SESSION_OPEN = NSE_OPEN
SESSION_LAST_BAR = time(15, 29)

@dataclass(frozen=True, order=True)
class OptionKey:
    """One option contract, by what it is rather than by a broker's symbol."""

    expiry: date
    strike: float
    kind: Kind

    def intrinsic(self, spot: float) -> float:
        if self.kind is Kind.CALL:
            return max(0.0, spot - self.strike)
        return max(0.0, self.strike - spot)

    def __str__(self) -> str:
        return f"{self.expiry:%d%b%y} {self.strike:g} {self.kind}"


@dataclass(frozen=True)
class Bar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass(frozen=True)
class Quote:
    """One contract at the last closed bar, as a strategy sees it."""

    key: OptionKey
    price: float
    volume: int
    oi: int


class View:
    """The market as of the last closed bar. All a strategy can see."""

    def __init__(
        self,
        source: MarketSource,
        day: date,
        bars: Sequence[Bar],
        context: Callable[[], Context] | None = None,
    ) -> None:
        self._history = source
        self._context = context
        self.day = day
        self._bars = bars
        self._i = -1
        self._sessions: dict[date, int] = {}

    @property
    def context(self) -> Context:
        """The run's daily context: pivots, gap, VIX."""
        if self._context is None:
            raise RuntimeError("this view was made without a daily context")
        return self._context()

    def _advance(self, i: int) -> None:
        """Engine only: bar `i` of the day has closed."""
        self._i = i

    @property
    def now(self) -> datetime:
        """The start of the last closed bar. A decision now fills in the next one."""
        return self._bars[self._i].ts

    @property
    def clock(self) -> time:
        """The time a decision is being made at: the close of the last bar."""
        ts = self.now
        return time(ts.hour, ts.minute + 1) if ts.minute < 59 else time(ts.hour + 1, 0)

    @property
    def session_start(self) -> time:
        """When today's first bar closed - the earliest a decision can be made."""
        first = self._bars[0].ts
        return (first + timedelta(minutes=1)).time()

    @property
    def bars_left(self) -> int:
        """Bars still to come today. Zero on the last one - which is not always
        15:29: a Saturday special session ended at 12:29."""
        return len(self._bars) - 1 - self._i

    def spot(self) -> float:
        return self._bars[self._i].close

    def bar(self) -> Bar:
        """The index's last closed bar, whole."""
        return self._bars[self._i]

    def sessions_before(self, count: int) -> list[list[Bar]]:
        """The index's bars for up to `count` sessions before today, oldest first.

        Earlier sessions only, so nothing here can be from a bar not yet closed.
        """
        if count <= 0:
            return []
        # Seven calendar days hold at least three sessions, even around Diwali.
        earliest = self.day - timedelta(days=count * 7 // 3 + 7)
        days = [d for d in self._history.trading_days(earliest, self.day) if d < self.day]
        return [bars for d in days[-count:] if (bars := self._history.index_day(d))]

    def expiries(self) -> list[date]:
        """Expiries not yet passed, nearest first."""
        return [e for e in self._history.expiries() if e >= self.day]

    def monthly_expiries(self) -> list[date]:
        """Monthly expiries not yet passed, nearest first."""
        return [e for e in self._history.monthly_expiries() if e >= self.day]

    def sessions_to(self, expiry: date) -> int:
        """Trading sessions after today up to `expiry`: 0 on the session it settles
        in, 1 on the one before.

        Sessions, not calendar days - the day before a Monday expiry is Friday. An
        expiry listed on a holiday settles the session before, and counts as 0
        there. Past the end of the data, weekdays stand in for sessions.
        """
        if expiry in self._sessions:
            return self._sessions[expiry]
        known = self._history.trading_days(self.day, expiry)
        count = len(known) - 1
        last = known[-1] if known else self.day
        if self._history.next_trading_day(last) is None:
            step = last + timedelta(days=1)
            while step <= expiry:
                count += step.weekday() < 5
                step += timedelta(days=1)
        self._sessions[expiry] = max(0, count)
        return self._sessions[expiry]

    def chain(self, expiry: date) -> list[Quote]:
        return self._history.chain_at(expiry, self.now)

    def last_trade(self, key: OptionKey) -> tuple[datetime, float] | None:
        """When one contract last traded, as of the last closed bar, and at what.

        Today's latest bar if it has traded today; otherwise its last bar from an
        earlier session. None if it never has.
        """
        bars = self._history.contract_day(key, self.day)
        bar = bars.get(self.now)
        if bar is not None:
            return self.now, bar.close
        earlier = [ts for ts in bars if ts < self.now]
        if earlier:
            at = max(earlier)
            return at, bars[at].close
        return self._history.prev_bar(key, self.day)

    def price(self, key: OptionKey) -> float | None:
        """Last traded price of one contract as of the last closed bar.

        This is a print, not a fill: an order still waits for the contract to
        actually trade. For what a position is worth, see `mark`.
        """
        last = self.last_trade(key)
        return None if last is None else last[1]

    def mark(self, key: OptionKey) -> float | None:
        """What one contract is worth now: its price on this bar if it traded on it,
        and otherwise its last price carried to the index level and time now.

        Carried however recent: the 13 Mar put last traded a minute before a
        700-point fall. With the index where it was, carrying changes nothing.

        An illiquid wing still has a value on a day it does not trade - the 19400
        CE of Jan 2024 did not trade on 4 of its 34 sessions - but not the value
        it had before a gap or a crash. See `optbt/marks.py`.
        """
        last = self.last_trade(key)
        if last is None:
            return None
        at, price = last
        if at == self.now:
            return price
        spot_then = self._spot_at(at)
        if spot_then is None:
            return price
        option_type: OptionType = "CE" if key.kind is Kind.CALL else "PE"
        return reprice(
            price,
            key.strike,
            option_type,
            spot_then=spot_then,
            years_then=years_to(key.expiry, at),
            spot_now=self.spot(),
            years_now=years_to(key.expiry, self.now),
        )

    def _spot_at(self, ts: datetime) -> float | None:
        if ts.date() == self.day:
            return next((b.close for b in self._bars if b.ts == ts), None)
        return self._history.close_at(self._history.index_symbol, ts)

    def lot_size(self, expiry: date) -> int:
        return self._history.lot_size(self.day, expiry)
