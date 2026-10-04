"""Fetching the option history that is not held yet.

Run it once and it fetches four years; run it again tomorrow and it fetches the
expiries that settled since. Every contract is asked for at most once - the store's
ledger says what is held, and an expired contract never changes - so repeating a
run, or resuming one that was interrupted, costs a few list requests and nothing
else.

The order of work:

  1. The expiry calendar, a year at a time. Cheap, and asked afresh every run.
  2. The index and India VIX at one minute, from wherever the store's copy ends.
     Fetched before the options because the strike band below is read from it.
  3. Each settled expiry, newest first, so a run stopped early still holds the
     most recent - and most useful - history.

Newest first also means an interrupted run's gap is at the old end, which is the
end a rerun reaches last. That is fine: nothing is ever fetched twice either way.

The strike band. Every strike ever listed is roughly twice the requests and twice
the disk of the strikes within 10% of where the index traded, and the far ones are
mostly minutes that never traded. `band` sets the cut; 0 keeps everything. Widening
it later fetches only the strikes the narrower run left out.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from optbt.data.models import Candle, Contract, Kind
from optbt.data.source import MAX_SPAN_DAYS, ExpiredSource, SourceError
from optbt.data.store import OptionStore
from venues.instruments import INDIA_VIX, OPTION_SERIES

log = logging.getLogger(__name__)

#: How far before an expiry the index range for its strike band is read. A weekly
#: lives about a month; a monthly about three.
BAND_LOOKBACK = timedelta(days=MAX_SPAN_DAYS)

#: Longest run of days without a session: a weekend plus a holiday either side.
LISTING_SLACK = timedelta(days=5)

#: Nothing is asked for before this. The expired endpoints were seen serving 2020;
#: a floor keeps a walk-back finite if a contract ever answers every window.
EARLIEST = date(2019, 1, 1)


@dataclass(frozen=True)
class ExpiryReport:
    expiry: date
    listed: int
    #: Inside the strike band (all futures are).
    wanted: int
    already_held: int
    fetched: int
    bars: int
    failed: tuple[str, ...]


def _year_spans(start: date, end: date) -> Iterator[tuple[date, date]]:
    """Spans of at most 366 days, the expiry endpoint's limit."""
    while start <= end:
        stop = min(end, start + timedelta(days=365))
        yield start, stop
        start = stop + timedelta(days=1)


def refresh_calendar(
    store: OptionStore, source: ExpiredSource, underlying: str, since: date, until: date
) -> tuple[list[date], list[date]]:
    options: set[date] = set()
    futures: set[date] = set()
    # The endpoint refuses a range reaching today ("range_to cannot be current date
    # or a future date"), and only settled expiries are wanted anyway.
    for start, stop in _year_spans(since, until - timedelta(days=1)):
        found = source.expiries(underlying, start, stop)
        options.update(found.options)
        futures.update(found.futures)
    store.write_expiries(underlying, "options", sorted(options))
    store.write_expiries(underlying, "futures", sorted(futures))
    return sorted(options), sorted(futures)


def refresh_index(
    store: OptionStore, source: ExpiredSource, symbol: str, since: date, until: date
) -> int:
    """One-minute bars from where the store's copy ends (that day again, since the
    last run may have caught it mid-session) up to `until`."""
    held = store.index_range(symbol, "1")
    start = max(since, held[1].date()) if held else since
    written = 0
    while start <= until:
        stop = min(until, start + timedelta(days=MAX_SPAN_DAYS))
        written += store.write_index(symbol, "1", source.index_candles(symbol, "1", start, stop))
        start = stop + timedelta(days=1)
    return written


def in_band(
    contracts: list[Contract],
    expiry: date,
    ranges: dict[date, tuple[float, float]],
    band: float,
) -> list[Contract]:
    """Futures, and options struck within `band` of where the index traded in the
    run-up to this expiry. Everything, if the index was not traded in that span as
    far as the store knows - a missing band is not a reason to fetch nothing."""
    if band <= 0:
        return contracts
    days = [r for d, r in ranges.items() if expiry - BAND_LOOKBACK <= d <= expiry]
    if not days:
        log.warning("no index history before %s; fetching every strike", expiry)
        return contracts
    low = min(r[0] for r in days) * (1 - band)
    high = max(r[1] for r in days) * (1 + band)
    return [
        c
        for c in contracts
        if c.kind is Kind.FUTURE or (c.strike is not None and low <= c.strike <= high)
    ]


def fetch_contract(
    source: ExpiredSource, contract: Contract, until: date | None = None
) -> list[Candle]:
    """Every bar the contract has, walking back from expiry a window at a time.

    Stops at the window that holds the first bar - a contract listed inside it has
    nothing earlier, so asking would be a wasted request. A weekly is one request;
    a quarterly listed years out is several.

    Deliberately not bounded by the run's `since`, which chooses expiries and not
    how much of each: the ledger records a contract as held once, so a contract
    stored cut short would stay cut short for good.
    """
    candles: list[Candle] = []
    # `until`: a contract still trading, asked for only as far as it has gone.
    end = contract.expiry if until is None else min(contract.expiry, until)
    while end >= EARLIEST:
        start = max(EARLIEST, end - timedelta(days=MAX_SPAN_DAYS))
        page = source.candles(contract.symbol, start, end)
        candles.extend(page)
        # Slack for a window that opens on a weekend or a holiday: its first bar is
        # a few days in without the contract having been listed inside it.
        if not page or page[0].ts.date() > start + LISTING_SLACK:
            break
        end = start - timedelta(days=1)
    return candles


def backfill(
    store: OptionStore,
    source: ExpiredSource,
    underlying: str,
    *,
    since: date,
    until: date,
    band: float,
    dry_run: bool = False,
    now: Callable[[], datetime] = datetime.now,
) -> Iterator[ExpiryReport]:
    """Fetch what is missing, reporting after each expiry."""
    options, futures = refresh_calendar(store, source, underlying, since, until)

    # Even on a dry run: a few dozen requests, and without it the band cannot be
    # drawn and the estimate would count every strike.
    index = OPTION_SERIES[underlying]
    for symbol in (index, INDIA_VIX):
        written = refresh_index(store, source, symbol, since, until)
        log.info("%s: %d one-minute bars written", symbol, written)
    ranges = store.daily_ranges(index)

    # Only settled expiries: today's is still trading, and a contract is ledgered
    # once and never revisited, so storing half a day of it would be permanent.
    settled = sorted({*options, *futures}, reverse=True)
    for expiry in (e for e in settled if since <= e < until):
        listed = source.contracts(underlying, expiry)
        wanted = in_band(listed, expiry, ranges, band)
        held = store.held([c.symbol for c in wanted])
        todo = [c for c in wanted if c.symbol not in held]
        fetched = bars = 0
        failed: list[str] = []
        if not dry_run:
            for contract in todo:
                try:
                    candles = fetch_contract(source, contract)
                except SourceError as exc:
                    # Not ledgered, so the next run asks again.
                    log.warning("%s: %s", contract.symbol, exc)
                    failed.append(contract.symbol)
                    continue
                bars += store.write_contract(contract, candles, fetched_at=now())
                fetched += 1
        yield ExpiryReport(
            expiry=expiry,
            listed=len(listed),
            wanted=len(wanted),
            already_held=len(held),
            fetched=fetched,
            bars=bars,
            failed=tuple(failed),
        )
