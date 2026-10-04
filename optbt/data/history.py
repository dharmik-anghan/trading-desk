"""The option store, read for a backtest: `optbt.source.MarketSource` over DuckDB.

Caches what a run asks for repeatedly - a day's index bars, a contract's bars
for a day - because a four-year run asks for the same day's spot hundreds of
times and each query, fast as it is, is still a round trip.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

import duckdb

from optbt.data.models import Kind
from optbt.market import SESSION_LAST_BAR, SESSION_OPEN, Bar, OptionKey, Quote
from venues.instruments import OPTION_SERIES

#: Every lot size each index has used. Per underlying, because a size from another
#: index can fit by coincidence: 120 (MIDCPNIFTY's) divides any NIFTY figure that
#: is a multiple of 600, and was picked for two 2025 days before this was split.
KNOWN_LOT_SIZES: dict[str, frozenset[int]] = {
    "NIFTY": frozenset({25, 50, 65, 75}),
    "BANKNIFTY": frozenset({15, 25, 30, 35}),
    "FINNIFTY": frozenset({25, 40, 65}),
    "MIDCPNIFTY": frozenset({50, 75, 120, 140}),
    "SENSEX": frozenset({10, 20}),
}

#: Share of a day's open-interest figures a lot size must divide to be the lot.
#: Not 95%: a monthly listed before a lot change carries positions in the old
#: size, and on 25 Mar and 29 Jun 2026 only 89-90% of the monthly's figures were
#: whole lots of 65 - the rest were from when it was 75, which fitted 6%. 80%
#: still separates the lot from the others by a wide margin.
LOT_VOTE = 0.80


@dataclass(frozen=True)
class ChainQuote:
    """One contract as of a moment: its last trade, and when that was."""

    key: OptionKey
    price: float
    last_at: datetime
    oi: int
    #: Contracts traded in the session so far.
    volume: int


@dataclass(frozen=True)
class Coverage:
    """What the store holds for one underlying, and the window a run can use."""

    #: The tradable window: days the index traded that option bars exist for.
    first_day: date | None
    last_day: date | None
    #: Settled expiries the calendar lists, and how many of them are held.
    expiries_listed: int
    expiries_held: int
    first_expiry: date | None
    last_expiry: date | None
    contracts: int
    bars: int


class History:
    """Read access to the option store. One underlying per instance.

    Caches what a run asks for repeatedly - a day's index bars, a contract's bars
    for a day - because a four-year run asks for the same day's spot hundreds of
    times and each query, fast as it is, is still a round trip.
    """

    def __init__(self, conn: duckdb.DuckDBPyConnection, underlying: str = "NIFTY") -> None:
        self._conn = conn
        self.underlying = underlying
        self.index_symbol = OPTION_SERIES[underlying]
        self._index: dict[date, list[Bar]] = {}
        self._contract: dict[tuple[OptionKey, date], dict[datetime, Bar]] = {}
        self._lots: dict[tuple[date, date], int] = {}
        self._prev: dict[tuple[OptionKey, date], tuple[datetime, float] | None] = {}
        self._expiries: list[date] | None = None
        self._monthlies: list[date] | None = None

    @classmethod
    def open(cls, path: Path | str, underlying: str = "NIFTY", *, wait: float = 30.0) -> History:
        """Read-only, and waiting out a writer: a backfill holds the file only for
        the moment of each write, so a backtest that arrives mid-write waits
        milliseconds rather than being refused."""
        from optbt.data.store import connect

        return cls(connect(str(path), read_only=True, wait=wait), underlying)

    def close(self) -> None:
        self._conn.close()

    def underlyings(self) -> list[str]:
        """Every underlying the store holds option bars for."""
        rows = self._conn.execute(
            "SELECT DISTINCT underlying FROM contract WHERE bars > 0 ORDER BY 1"
        ).fetchall()
        return [r[0] for r in rows]

    def coverage(self) -> Coverage:
        held = self._conn.execute(
            "SELECT count(DISTINCT expiry), min(expiry), max(expiry), count(*), "
            "coalesce(sum(bars), 0) FROM contract WHERE underlying = ? AND kind <> 'FUT'",
            [self.underlying],
        ).fetchone()
        # Not the first option bar - a long-dated contract's listing reaches back
        # to 2021, eighteen months before any index data, which made that the
        # default start of every run.
        days = self._conn.execute(
            "SELECT greatest(min(CAST(i.ts AS DATE)), (SELECT min(CAST(ts AS DATE)) "
            "FROM option_bar WHERE underlying = ?)), least(max(CAST(i.ts AS DATE)), "
            "(SELECT max(CAST(ts AS DATE)) FROM option_bar WHERE underlying = ?)) "
            "FROM index_bar i WHERE i.symbol = ?",
            [self.underlying, self.underlying, self.index_symbol],
        ).fetchone()
        listed = self._conn.execute(
            "SELECT count(*) FROM expiry WHERE underlying = ? AND kind = 'options' "
            "AND expiry < current_date",
            [self.underlying],
        ).fetchone()
        assert held is not None and days is not None and listed is not None
        return Coverage(
            first_day=days[0],
            last_day=days[1],
            expiries_listed=int(listed[0]),
            expiries_held=int(held[0]),
            first_expiry=held[1],
            last_expiry=held[2],
            contracts=int(held[3]),
            bars=int(held[4]),
        )

    # ------------------------------------------------------------- calendar

    def trading_days(self, start: date, end: date) -> list[date]:
        rows = self._conn.execute(
            "SELECT DISTINCT CAST(ts AS DATE) AS d FROM index_bar "
            "WHERE symbol = ? AND ts >= ? AND ts < ? + INTERVAL 1 DAY ORDER BY d",
            [self.index_symbol, start, end],
        ).fetchall()
        return [row[0] for row in rows]

    def next_trading_day(self, day: date) -> date | None:
        """The first session after `day`, or None past the end of the data."""
        row = self._conn.execute(
            "SELECT min(CAST(ts AS DATE)) FROM index_bar "
            "WHERE symbol = ? AND ts >= ? + INTERVAL 1 DAY",
            [self.index_symbol, day],
        ).fetchone()
        return row[0] if row and row[0] is not None else None

    def expiries(self) -> list[date]:
        """Every option expiry the exchange listed, ascending - held or not.

        From the calendar, not from what has been fetched. Asking only what is
        held would make "the nearest expiry" silently mean "the nearest one we
        happen to have", and a straddle would trade next week's contracts on a
        day whose own weekly was never downloaded. With the calendar, that day's
        chain comes back empty and the day is skipped, visibly.
        """
        if self._expiries is None:
            rows = self._conn.execute(
                "SELECT expiry FROM expiry WHERE underlying = ? AND kind = 'options' "
                "ORDER BY expiry",
                [self.underlying],
            ).fetchall()
            self._expiries = [row[0] for row in rows]
        return self._expiries

    def monthly_expiries(self) -> list[date]:
        """The monthly expiries: those futures expire on too."""
        if self._monthlies is None:
            rows = self._conn.execute(
                "SELECT expiry FROM expiry WHERE underlying = ? AND kind = 'futures' "
                "ORDER BY expiry",
                [self.underlying],
            ).fetchall()
            self._monthlies = [row[0] for row in rows]
        return self._monthlies

    # ----------------------------------------------------------------- index

    def daily(self, symbol: str) -> list[tuple[date, float, float, float, float]]:
        """`symbol`'s sessions as (day, open, high, low, close), from its minute bars."""
        rows = self._conn.execute(
            """
            SELECT CAST(ts AS DATE) AS d, arg_min(open, ts), max(high), min(low), arg_max(close, ts)
            FROM index_bar WHERE symbol = ? AND resolution = '1'
            AND CAST(ts AS TIME) BETWEEN ? AND ?
            GROUP BY d ORDER BY d
            """,
            [symbol, SESSION_OPEN, SESSION_LAST_BAR],
        ).fetchall()
        return [(d, float(o), float(h), float(lo), float(c)) for d, o, h, lo, c in rows]

    def close_at(self, symbol: str, ts: datetime) -> float | None:
        """`symbol`'s close at the bar named `ts`, or the last one before it that day."""
        row = self._conn.execute(
            "SELECT close FROM index_bar WHERE symbol = ? AND resolution = '1' "
            "AND ts <= ? AND ts >= ? ORDER BY ts DESC LIMIT 1",
            [symbol, ts, datetime.combine(ts.date(), time(0))],
        ).fetchone()
        return float(row[0]) if row else None


    def index_day(self, day: date) -> list[Bar]:
        """The index's session bars for a day, in order."""
        if day not in self._index:
            rows = self._conn.execute(
                "SELECT ts, open, high, low, close, volume FROM index_bar "
                "WHERE symbol = ? AND resolution = '1' AND ts >= ? AND ts <= ? ORDER BY ts",
                [
                    self.index_symbol,
                    datetime.combine(day, SESSION_OPEN),
                    datetime.combine(day, SESSION_LAST_BAR),
                ],
            ).fetchall()
            self._index = {day: [Bar(*row) for row in rows]}  # one day held at a time
        return self._index[day]

    # --------------------------------------------------------------- options

    def contract_day(self, key: OptionKey, day: date) -> dict[datetime, Bar]:
        """A contract's session bars for a day, by minute."""
        cache_key = (key, day)
        if cache_key not in self._contract:
            if len(self._contract) > 512:
                self._contract.clear()
            rows = self._conn.execute(
                "SELECT ts, open, high, low, close, volume FROM option_bar "
                "WHERE underlying = ? AND expiry = ? AND strike = ? AND kind = ? "
                "AND ts >= ? AND ts <= ?",
                [
                    self.underlying,
                    key.expiry,
                    key.strike,
                    str(key.kind),
                    datetime.combine(day, SESSION_OPEN),
                    datetime.combine(day, SESSION_LAST_BAR),
                ],
            ).fetchall()
            self._contract[cache_key] = {row[0]: Bar(*row) for row in rows}
        return self._contract[cache_key]

    def prev_bar(self, key: OptionKey, day: date) -> tuple[datetime, float] | None:
        """When the contract last traded before `day`'s session, and at what close;
        None if it never had. Cached: asked every minute of a day it is quiet."""
        cache_key = (key, day)
        if cache_key not in self._prev:
            row = self._conn.execute(
                "SELECT ts, close FROM option_bar WHERE underlying = ? AND expiry = ? "
                "AND strike = ? AND kind = ? AND ts < ? ORDER BY ts DESC LIMIT 1",
                [self.underlying, key.expiry, key.strike, str(key.kind), day],
            ).fetchone()
            self._prev[cache_key] = (row[0], float(row[1])) if row else None
        return self._prev[cache_key]

    def chain_at(self, expiry: date, ts: datetime) -> list[Quote]:
        """Every contract of one expiry, at the close of the bar named `ts`."""
        rows = self._conn.execute(
            "SELECT strike, kind, close, volume, oi FROM option_bar "
            "WHERE underlying = ? AND expiry = ? AND ts = ? AND kind <> 'FUT' "
            "ORDER BY strike, kind",
            [self.underlying, expiry, ts],
        ).fetchall()
        return [
            Quote(OptionKey(expiry, float(r[0]), Kind(r[1])), float(r[2]), int(r[3]), int(r[4]))
            for r in rows
        ]

    # ----------------------------------------------------- as of a moment

    #: How far back an "as of" price may come from. A contract that has not
    #: traded in four calendar days - a long weekend - has no price worth showing.
    ASOF_LOOKBACK = timedelta(days=4)

    def chain_asof(self, expiry: date, ts: datetime) -> list[ChainQuote]:
        """Every contract of one expiry as it stood at the close of the bar
        named `ts`: its last trade at or before then, the open interest that
        trade carried, and the session's volume so far.

        Unlike `chain_at`, a strike that did not trade in that very minute is
        still there, at its last price - with `last_at` saying how old it is.
        """
        day_start = datetime.combine(ts.date(), SESSION_OPEN)
        rows = self._conn.execute(
            "SELECT strike, kind, arg_max(close, ts), max(ts), arg_max(oi, ts), "
            "coalesce(sum(volume) FILTER (WHERE ts >= ?), 0) "
            "FROM option_bar WHERE underlying = ? AND expiry = ? AND kind <> 'FUT' "
            "AND ts <= ? AND ts >= ? GROUP BY strike, kind ORDER BY strike, kind",
            [day_start, self.underlying, expiry, ts, ts - self.ASOF_LOOKBACK],
        ).fetchall()
        return [
            ChainQuote(
                OptionKey(expiry, float(r[0]), Kind(r[1])),
                float(r[2]),
                r[3],
                int(r[4]),
                int(r[5]),
            )
            for r in rows
        ]

    def price_asof(self, key: OptionKey, ts: datetime) -> tuple[datetime, float] | None:
        """One contract's last trade at or before the close of the bar named `ts`."""
        row = self._conn.execute(
            "SELECT ts, close FROM option_bar WHERE underlying = ? AND expiry = ? "
            "AND strike = ? AND kind = ? AND ts <= ? AND ts >= ? ORDER BY ts DESC LIMIT 1",
            [self.underlying, key.expiry, key.strike, str(key.kind), ts, ts - self.ASOF_LOOKBACK],
        ).fetchone()
        return (row[0], float(row[1])) if row else None

    def future_asof(self, ts: datetime) -> tuple[date, float] | None:
        """The nearest futures contract's last trade at or before `ts`, and its expiry."""
        row = self._conn.execute(
            "SELECT expiry, close FROM option_bar WHERE underlying = ? AND kind = 'FUT' "
            "AND expiry >= ? AND ts <= ? AND ts >= ? ORDER BY expiry, ts DESC LIMIT 1",
            [self.underlying, ts.date(), ts, ts - self.ASOF_LOOKBACK],
        ).fetchone()
        return (row[0], float(row[1])) if row else None

    def lot_size(self, day: date, expiry: date) -> int:
        """The lot size in force on a day, read from the data rather than a table.

        Open interest and volume are whole lots, so the lot is the largest known
        size that divides most of a day's figures. A table typed from memory is
        how every trade in a run ends up mis-sized: NIFTY has been 75, 50, 25, 75
        and 65 inside four years.

        Two sources, because each is unreliable in its own way:

        - Open interest carries positions opened under an earlier lot. In the
          week of the 27 Mar 2025 monthly, 6% of its figures were still lots of
          25 from 2024 - so 75 fitted only 94%, and a strict test picked 25,
          sizing those trades at a third.
        - Volume is only ever new trades, so always the current lot - except
          that Fyers' volume in 2026 has stray figures (45,502 on 3 Jul 2026),
          and on 25 Mar 2026 only 60% of it divided by 65.

        So each size is judged by whichever source supports it better, and the
        largest size either source puts past LOT_VOTE wins.
        """
        if (day, expiry) not in self._lots:
            known = KNOWN_LOT_SIZES[self.underlying]
            lot = _vote_lot(self._figures(day, expiry), known)
            if lot is None:
                # Too little on this expiry today - the day's contracts together,
                # dominated by the near expiries, still say what a trade is sized in.
                lot = _vote_lot(self._figures(day, None), known)
            if lot is None:
                raise ValueError(f"no lot size fits the open interest on {day} for {expiry}")
            self._lots[(day, expiry)] = lot
        return self._lots[(day, expiry)]

    def _figures(self, day: date, expiry: date | None) -> tuple[list[int], list[int]]:
        """The day's distinct open-interest and volume figures."""
        where = "underlying = ? AND kind <> 'FUT' AND ts >= ? AND ts < ? + INTERVAL 1 DAY"
        params: list[object] = [self.underlying, day, day]
        if expiry is not None:
            where += " AND expiry = ?"
            params.append(expiry)
        oi = self._conn.execute(
            f"SELECT DISTINCT oi FROM option_bar WHERE {where} AND oi > 0", params
        ).fetchall()
        volume = self._conn.execute(
            f"SELECT DISTINCT volume FROM option_bar WHERE {where} AND volume > 0", params
        ).fetchall()
        return [r[0] for r in oi], [r[0] for r in volume]


def _vote_lot(
    figures: tuple[Sequence[int], Sequence[int]], known: frozenset[int]
) -> int | None:
    """The largest known lot that divides at least LOT_VOTE of the open interest
    figures or of the volume figures, whichever supports it better."""

    def share(values: Sequence[int], lot: int) -> float:
        return sum(1 for v in values if v % lot == 0) / len(values) if values else 0.0

    oi, volume = figures
    for lot in sorted(known, reverse=True):
        if max(share(oi, lot), share(volume, lot)) >= LOT_VOTE:
            return lot
    return None
