"""Everything a backtest may ask of the market, and nothing about where it lives.

The engine, the strategy's `View` and the daily context read the market only
through this, so the store behind it - DuckDB today, `optbt/data/history.py` -
can change without them, and an engine written in another language implements
the same questions against the same store. It is a narrow interface on purpose:
a run reads a whole chain only at the moment it enters and then only the few
contracts it holds, so asking is far cheaper than shipping the market across up
front (measured: preloading four years of weekly windows took longer than a run).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Protocol

from optbt.market import Bar, OptionKey, Quote


class MarketSource(Protocol):
    """One underlying's history: calendar, index, chains, contracts."""

    underlying: str
    #: The index the options are written on, as the store names it.
    index_symbol: str

    # ----------------------------------------------------------- calendar

    def trading_days(self, start: date, end: date) -> list[date]: ...

    def next_trading_day(self, day: date) -> date | None:
        """The first session after `day`, or None past the end of the data."""
        ...

    def expiries(self) -> list[date]:
        """Every option expiry the exchange listed, ascending - held or not."""
        ...

    def monthly_expiries(self) -> list[date]: ...

    # -------------------------------------------------------------- index

    def index_day(self, day: date) -> list[Bar]:
        """The index's session bars for a day, in order."""
        ...

    def future_day(self, day: date) -> list[Bar]:
        """The near-month future's session bars for a day; empty when not stored."""
        ...

    def daily(self, symbol: str) -> list[tuple[date, float, float, float, float]]:
        """`symbol`'s sessions as (day, open, high, low, close)."""
        ...

    def close_at(self, symbol: str, ts: datetime) -> float | None:
        """`symbol`'s close at the bar named `ts`, or the last one before it that day."""
        ...

    # ------------------------------------------------------------ options

    def chain_at(self, expiry: date, ts: datetime) -> list[Quote]:
        """Every contract of one expiry, at the close of the bar named `ts`."""
        ...

    def contract_day(self, key: OptionKey, day: date) -> dict[datetime, Bar]:
        """A contract's session bars for a day, by minute."""
        ...

    def prev_bar(self, key: OptionKey, day: date) -> tuple[datetime, float] | None:
        """When the contract last traded before `day`'s session, and its close; or None."""
        ...

    def lot_size(self, day: date, expiry: date) -> int:
        """The lot in force on `day` for `expiry`. Raises ValueError when unknown."""
        ...
