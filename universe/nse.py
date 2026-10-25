"""Indian indices, their sectors, and who is in them.

Two kinds of thing are listed here and they are not the same:

  A **broad index** - Nifty 50, Bank Nifty - whose rotation graph plots its own
  constituent stocks against it.

  A **sector index** - Nifty IT, Nifty Pharma - which is itself a thing to plot,
  against a broad benchmark. "All sectors" is the view that plots every one of
  them against the Nifty 50, and it is the view most people mean by an RRG.

Symbols are given in Fyers' spelling because that is the desk's source for
Indian data, and the index list file names in NSE's, because that is where
membership comes from. Both are written out rather than derived, and both were
checked against the live source rather than assumed, because neither naming is a
rule - each is a history.

NSE's list names: `ind_niftyfinancelist` for financial services,
`ind_nifty_privatebanklist` with an underscore where its neighbours have none,
`ind_niftymidcapselect_list` with the underscore somewhere else again. Guessing
produced an HTML error page three times out of fifteen.

Fyers' index symbols: `NIFTYCONSRDURBL` for consumer durables, `NIFTYOILANDGAS`
spelled out where the list file says `oilgas`, and `NIFTYFINSRV2550` for the
financial services index. Three of twenty were not what they looked like.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import requests

import paths

log = logging.getLogger(__name__)

LISTS_URL = "https://nsearchives.nseindia.com/content/indices/{file}.csv"
TIMEOUT = 25.0

#: NSE serves an HTML error page rather than a 404 for a name it does not know,
#: so a fetch is checked against what a constituent list actually starts with.
HEADER = "Company Name"


@dataclass(frozen=True)
class IndexSpec:
    """One index: what to call it, how to price it, and where its members are."""

    id: str
    name: str
    #: As the desk's Indian source spells it.
    symbol: str
    #: NSE's file name for the constituent list, when it publishes one.
    members_file: str | None = None
    #: True for a sector index, which is something to plot rather than a
    #: benchmark to plot against.
    is_sector: bool = False


#: The benchmark everything is measured against unless told otherwise.
DEFAULT_BENCHMARK = "NIFTY50"


INDICES: tuple[IndexSpec, ...] = (
    # --- broad indices: plot their own constituents ------------------------
    IndexSpec("NIFTY50", "Nifty 50", "NSE:NIFTY50-INDEX", "ind_nifty50list"),
    IndexSpec("BANKNIFTY", "Bank Nifty", "NSE:NIFTYBANK-INDEX", "ind_niftybanklist"),
    IndexSpec("FINNIFTY", "Fin Nifty", "NSE:FINNIFTY-INDEX", "ind_niftyfinancelist"),
    IndexSpec(
        "MIDCPNIFTY", "Midcap Select", "NSE:MIDCPNIFTY-INDEX", "ind_niftymidcapselect_list"
    ),
    IndexSpec("NIFTYMIDCAP100", "Nifty Midcap 100", "NSE:NIFTYMIDCAP100-INDEX",
              "ind_niftymidcap100list"),
    #: The Sensex is a BSE index and NSE publishes no list for it, so it can be a
    #: benchmark but has no constituents to plot. Saying so is better than
    #: offering a list that would quietly be the wrong thirty stocks.
    IndexSpec("SENSEX", "Sensex", "BSE:SENSEX-INDEX", None),
    # --- sector indices: things to plot ------------------------------------
    IndexSpec("NIFTYIT", "IT", "NSE:NIFTYIT-INDEX", "ind_niftyitlist", is_sector=True),
    IndexSpec("NIFTYPHARMA", "Pharma", "NSE:NIFTYPHARMA-INDEX", "ind_niftypharmalist",
              is_sector=True),
    IndexSpec("NIFTYAUTO", "Auto", "NSE:NIFTYAUTO-INDEX", "ind_niftyautolist", is_sector=True),
    IndexSpec("NIFTYFMCG", "FMCG", "NSE:NIFTYFMCG-INDEX", "ind_niftyfmcglist", is_sector=True),
    IndexSpec("NIFTYMETAL", "Metal", "NSE:NIFTYMETAL-INDEX", "ind_niftymetallist",
              is_sector=True),
    IndexSpec("NIFTYREALTY", "Realty", "NSE:NIFTYREALTY-INDEX", "ind_niftyrealtylist",
              is_sector=True),
    IndexSpec("NIFTYENERGY", "Energy", "NSE:NIFTYENERGY-INDEX", "ind_niftyenergylist",
              is_sector=True),
    IndexSpec("NIFTYPSUBANK", "PSU Bank", "NSE:NIFTYPSUBANK-INDEX", "ind_niftypsubanklist",
              is_sector=True),
    IndexSpec("NIFTYPVTBANK", "Private Bank", "NSE:NIFTYPVTBANK-INDEX",
              "ind_nifty_privatebanklist", is_sector=True),
    IndexSpec("NIFTYMEDIA", "Media", "NSE:NIFTYMEDIA-INDEX", "ind_niftymedialist",
              is_sector=True),
    IndexSpec("NIFTYCONSUMPTION", "Consumer Durables", "NSE:NIFTYCONSRDURBL-INDEX",
              "ind_niftyconsumerdurableslist", is_sector=True),
    IndexSpec("NIFTYOILGAS", "Oil & Gas", "NSE:NIFTYOILANDGAS-INDEX", "ind_niftyoilgaslist",
              is_sector=True),
    IndexSpec("NIFTYHEALTHCARE", "Healthcare", "NSE:NIFTYHEALTHCARE-INDEX",
              "ind_niftyhealthcarelist", is_sector=True),
    IndexSpec("NIFTYFINSERVICE", "Financial Services", "NSE:NIFTYFINSRV2550-INDEX",
              "ind_niftyfinancialservices25_50list", is_sector=True),
)

BY_ID = {spec.id: spec for spec in INDICES}


def index(index_id: str) -> IndexSpec | None:
    return BY_ID.get(index_id.strip().upper())


def sectors() -> tuple[IndexSpec, ...]:
    """Every sector index. What "all sectors" plots."""
    return tuple(spec for spec in INDICES if spec.is_sector)


@dataclass(frozen=True)
class Member:
    """One constituent."""

    symbol: str
    name: str
    industry: str


@dataclass(frozen=True)
class Membership:
    """Who was in an index, and when that was established.

    Dated, and the date matters more than it looks. Membership changes, and a
    rotation graph drawn over two years using today's list is measuring a
    universe that did not exist then - the stocks that fell out are missing and
    the ones that came in were not there. It flatters, because indices add what
    has done well. Nothing here fixes that; the date is recorded so a reader
    knows which list they are looking at.
    """

    index_id: str
    at: datetime
    members: tuple[Member, ...]


def parse_list(text: str, index_id: str, at: datetime | None = None) -> Membership | None:
    """One of NSE's constituent CSVs.

    None when the text is not one - which is what an unknown file name gets,
    since NSE answers those with a web page rather than a 404.
    """
    if not text.lstrip().startswith(HEADER):
        return None
    rows = list(csv.DictReader(io.StringIO(text)))
    members = tuple(
        Member(
            symbol=str(row.get("Symbol", "")).strip(),
            name=str(row.get("Company Name", "")).strip(),
            industry=str(row.get("Industry", "")).strip(),
        )
        for row in rows
        if str(row.get("Symbol", "")).strip()
    )
    if not members:
        return None
    return Membership(index_id=index_id, at=at or datetime.now(UTC), members=members)


def fetch(spec: IndexSpec, session: requests.Session | None = None) -> Membership | None:
    """Ask NSE who is in this index. None when it publishes no list for it."""
    if spec.members_file is None:
        return None
    http = session or requests.Session()
    try:
        response = http.get(
            LISTS_URL.format(file=spec.members_file),
            timeout=TIMEOUT,
            # NSE serves nothing to a client that does not look like a browser.
            headers={"User-Agent": "Mozilla/5.0", "Accept": "text/csv,*/*"},
        )
    except requests.RequestException as exc:
        log.warning("could not reach NSE for %s: %s", spec.id, exc)
        return None
    if response.status_code >= 400:
        return None
    return parse_list(response.text, spec.id)


#: Tickers Fyers renamed before NSE's index lists did.
FYERS_RENAMED = {"ICICIPRULI": "ICICILIFE"}


def fyers_symbol(member: Member) -> str:
    """What the desk's Indian source calls this stock."""
    return f"NSE:{FYERS_RENAMED.get(member.symbol, member.symbol)}-EQ"


# ---------------------------------------------------------------------------
# On disk
# ---------------------------------------------------------------------------

DEFAULT_PATH = paths.CONSTITUENTS


def save(memberships: dict[str, Membership], path: Path = DEFAULT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        index_id: {
            "at": held.at.isoformat(),
            "members": [
                {"symbol": m.symbol, "name": m.name, "industry": m.industry}
                for m in held.members
            ],
        }
        for index_id, held in memberships.items()
    }
    path.write_text(json.dumps(payload, indent=1, sort_keys=True))


def load(path: Path = DEFAULT_PATH) -> dict[str, Membership]:
    """What was last fetched. Empty when nothing has been."""
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    out: dict[str, Membership] = {}
    for index_id, held in payload.items():
        try:
            out[index_id] = Membership(
                index_id=index_id,
                at=datetime.fromisoformat(held["at"]),
                members=tuple(
                    Member(
                        symbol=m["symbol"],
                        name=m.get("name", ""),
                        industry=m.get("industry", ""),
                    )
                    for m in held["members"]
                ),
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out


def constituents(index_id: str, path: Path = DEFAULT_PATH) -> Membership | None:
    """Who is in this index, from what was last fetched."""
    return load(path).get(index_id.strip().upper())


#: Series the desk needs that are in no index's constituent list.
#:
#: India VIX is the reason this exists. It is not an index with members and it
#: is not a stock, but it is the one series that makes an implied volatility
#: reading mean anything - a figure of 12 says nothing until you know the last
#: two years ran between 9 and 28.
EXTRAS: tuple[tuple[str, str], ...] = (("INDIAVIX", "NSE:INDIAVIX-INDEX"),)


def daily_series(path: Path = DEFAULT_PATH) -> list[tuple[str, str]]:
    """Every series kept as daily bars, as (label, Fyers symbol), indices first.

    Indices first because they are the benchmarks: a run interrupted early
    leaves the rotation graph able to draw sectors against Nifty even with no
    stocks.
    """
    out: list[tuple[str, str]] = [(spec.id, spec.symbol) for spec in INDICES]
    out.extend(EXTRAS)
    seen = {symbol for _, symbol in out}
    for membership in load(path).values():
        for member in membership.members:
            symbol = fyers_symbol(member)
            if symbol not in seen:
                seen.add(symbol)
                out.append((member.symbol, symbol))
    return out
