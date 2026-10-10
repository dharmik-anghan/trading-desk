"""Liveness, and what this desk can trade.

`/api/venues` is how a client learns which desks exist and what each supports,
rather than having the list hardcoded in the frontend. Capabilities are what
panels should key off: an option-chain panel means nothing on a perpetuals
venue, and asking beats assuming when a second desk arrives.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from fastapi import APIRouter, Request
from pydantic import BaseModel

from api.deps import HolidaysDep, OptionsVenueDep, get_broker
from broker.errors import BrokerError
from venues import listed
from venues.calendar import IST, in_session, next_open, session_bounds

router = APIRouter()


def nse_status(now: datetime, holidays: frozenset[date]) -> dict[str, object]:
    """Whether the NSE is trading, when it closes if so, and when it next opens.

    The options desk keys its polling off this: nothing it shows can change on a
    Saturday, a holiday or at night, so there is nothing to ask the broker for.
    """
    open_now = in_session(now, holidays)
    closes = session_bounds(now.astimezone(IST).date())[1]
    return {
        "open": open_now,
        "closes_at": closes.isoformat() if open_now else None,
        "next_open": None if open_now else next_open(now, holidays).isoformat(),
    }


@router.get("/api/health")
def health(
    request: Request, venue: OptionsVenueDep, holidays: HolidaysDep
) -> dict[str, object]:
    """Liveness, plus whether broker reads are currently degraded.

    Polled rarely; the per-request status codes above are what the desk reacts
    to. This is for the case where cached values are being served over a rate
    limit and every request still looks like a success.

    Answers with the broker logged out too: it is also what tells the desk the
    NSE is open, and an expired login is said by every other panel already.
    """
    try:
        override = request.app.dependency_overrides.get(get_broker)
        broker = override() if override else get_broker(venue)
    except BrokerError:
        broker = None
    since = getattr(broker, "seconds_since_rate_limited", None)
    recently = since is not None and since < 60
    return {
        "status": "ok",
        "rate_limited": recently,
        "nse": nse_status(datetime.now(UTC), holidays.dates()),
    }


class VenueResponse(BaseModel):
    id: str
    name: str
    asset_class: str
    quote_currency: str
    session: str
    capabilities: list[str]


@router.get("/api/venues", response_model=list[VenueResponse])
def venues() -> list[VenueResponse]:
    """Every venue the desk knows about, in the order a switcher should show them."""
    return [
        VenueResponse(
            id=spec.id,
            name=spec.name,
            asset_class=str(spec.asset_class),
            quote_currency=spec.quote_currency,
            session=str(spec.session),
            # sorted, so the response is stable rather than set-ordered
            capabilities=sorted(str(c) for c in spec.capabilities),
        )
        for spec in listed()
    ]
