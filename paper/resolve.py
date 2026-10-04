"""A strategy's legs turned into contracts on the live chain now.

The same spec a backtest runs - a template or a saved strategy - names its
strikes by rule ("2 out", "0.30 delta", "a quarter of the straddle") and its
expiry by choice ("the nearest weekly"). Here those are answered against the
market as it is this second, with the backtest's own `pick_strike`, so a
template means the same thing live as it did over history.

What a backtest also does with a spec but a single placement cannot - entry
times, re-entries, day filters, adjustments - is not applied: this places the
legs once, now.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from optbt.data.models import Kind as OptKind
from optbt.engine import Level, Side
from optbt.market import OptionKey, Quote
from optbt.strategies.legs import DAYS_SLACK, ExpiryChoice, LegsConfig, LegSpec, pick_strike
from paper.markets import LiveChain, LiveExpiry, LiveQuote
from venues.calendar import IST, NSE_CLOSE


@dataclass(frozen=True)
class DraftLeg:
    """One leg, priced on the live chain - or, with `error`, why it could not be."""

    side: Literal["buy", "sell"]
    kind: Literal["CE", "PE"]
    qty: float
    symbol: str | None
    strike: float | None
    expiry: date | None
    expiry_token: str | None
    bid: float | None
    ask: float | None
    mark: float | None
    #: As the spec has them: a share of the fill, or points from it.
    stop: Level | None
    target: Level | None
    error: str | None


def pick_live_expiry(
    expiries: list[LiveExpiry], choice: ExpiryChoice, now: datetime, *, fridays_weekly: bool
) -> LiveExpiry | None:
    """The expiry a choice means now. See `optbt.strategies.legs.ExpiryChoice`.

    `min_left` counts calendar days here, not sessions: a market that never
    closes has no sessions to count, and on the NSE the difference is a weekend.
    """
    today = now.astimezone(IST).date()
    alive = [
        e
        for e in expiries
        if e.delivery > now and (choice.min_left == 0 or (e.expiry - today).days >= choice.min_left)
    ]
    if choice.series == "days":
        monthly = [e for e in alive if e.monthly]
        if not monthly:
            return None
        best = min(
            monthly,
            key=lambda e: (abs((e.expiry - today).days - choice.days), -e.expiry.toordinal()),
        )
        return best if abs((best.expiry - today).days - choice.days) <= DAYS_SLACK else None
    if choice.series == "monthly":
        found = [e for e in alive if e.monthly]
    elif choice.series == "weekly" and fridays_weekly:
        found = [e for e in alive if e.expiry.weekday() == 4]
    else:
        found = alive
    return found[choice.nth - 1] if len(found) >= choice.nth else None


def _quotes(chain: LiveChain) -> list[Quote]:
    out = []
    for row in chain.rows:
        for kind, side in ((OptKind.CALL, row.ce), (OptKind.PUT, row.pe)):
            if side is None:
                continue
            _, q = side
            price = q.mark or q.ltp
            if price:
                out.append(
                    Quote(
                        OptionKey(chain.expiry.expiry, row.strike, kind),
                        price,
                        int(q.volume),
                        int(q.oi),
                    )
                )
    return out


def model_clock(now: datetime, delivery: datetime) -> datetime:
    """`now` as the backtest's option maths reads it: a naive IST time, measured
    to the NSE's 15:30 close. A venue that settles at another hour - Shark at
    13:30 IST - is handed a clock shifted by the difference, so an hour left to
    its delivery reads as an hour left to the close."""
    local = delivery.astimezone(IST)
    gap = datetime.combine(local.date(), NSE_CLOSE) - local.replace(tzinfo=None)
    return now.astimezone(IST).replace(tzinfo=None) + gap


def resolve(
    config: LegsConfig,
    chain_for: dict[str, LiveChain],
    expiries: list[LiveExpiry],
    now: datetime,
    *,
    fridays_weekly: bool,
    load: Callable[[str], LiveChain],
) -> list[DraftLeg]:
    """Each leg of `config` on the live chain.

    `load(token)` reads one expiry's chain; `chain_for` holds those already read,
    so legs sharing an expiry share one read.
    """
    out: list[DraftLeg] = []
    for spec in config.legs:
        side: Literal["buy", "sell"] = "buy" if spec.side is Side.BUY else "sell"
        kind: Literal["CE", "PE"] = "CE" if spec.kind is OptKind.CALL else "PE"

        expiry = pick_live_expiry(
            expiries, spec.expiry or config.expiry, now, fridays_weekly=fridays_weekly
        )
        if expiry is None:
            out.append(_failed(side, kind, spec, "no expiry listed that fits"))
            continue
        chain = chain_for.get(expiry.token)
        if chain is None:
            chain = chain_for[expiry.token] = load(expiry.token)
        qty = spec.lots * chain.step
        strike, why = pick_strike(
            _quotes(chain),
            chain.reference,
            spec.kind,
            spec.strike,
            model_clock(now, expiry.delivery),
        )
        if strike is None:
            out.append(_failed(side, kind, spec, why, qty))
            continue
        row = next((r for r in chain.rows if r.strike == strike), None)
        found = (row.ce if kind == "CE" else row.pe) if row is not None else None
        if found is None:
            out.append(_failed(side, kind, spec, "strike not quoted", qty))
            continue
        symbol, q = found
        out.append(_draft(side, kind, qty, symbol, strike, expiry, q, spec.stop, spec.target))
    return out


def _failed(
    side: Literal["buy", "sell"],
    kind: Literal["CE", "PE"],
    spec: LegSpec,
    why: str,
    qty: float = 0.0,
) -> DraftLeg:
    return DraftLeg(
        side=side,
        kind=kind,
        qty=qty,
        symbol=None,
        strike=None,
        expiry=None,
        expiry_token=None,
        bid=None,
        ask=None,
        mark=None,
        stop=spec.stop,
        target=spec.target,
        error=why,
    )


def _draft(
    side: Literal["buy", "sell"],
    kind: Literal["CE", "PE"],
    qty: float,
    symbol: str,
    strike: float,
    expiry: LiveExpiry,
    q: LiveQuote,
    stop: Level | None,
    target: Level | None,
) -> DraftLeg:
    return DraftLeg(
        side,
        kind,
        qty,
        symbol,
        strike,
        expiry.expiry,
        expiry.token,
        q.bid,
        q.ask,
        q.mark,
        stop,
        target,
        None,
    )
