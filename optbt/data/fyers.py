"""Fyers' expired F&O endpoints.

The ordinary `history()` call answers "Invalid symbol provided" for any contract
that has expired. Fyers serves those from three separate endpoints, marked beta:

    expiry_dates                 which expiries an underlying had, a year at a time
    history_underlying_symbols   every contract listed for one expiry
    fno_historical_data          that contract's candles, with open interest

What was established against the live API before this was written (2026-09-28):

  - One-minute bars go back to at least 2020.
  - One request may span at most 100 days at intraday resolutions, and at most
    366 days for the expiry list.
  - A bar is returned for every minute of the contract's life, traded or not. A
    minute with no trade has zero volume and repeats the last price - so volume,
    not the presence of a row, is what says a price was real.
  - The response has iv/delta/gamma/theta/vega columns, and every one of them was
    null in every sample. They are not stored; the engine computes its own.

Fyers allows 10 requests a second and 200 a minute. The minute is the binding one
over a long run, so requests are spaced to stay under it rather than bursting to
the per-second limit and then being refused.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from broker.errors import AuthFailed, BrokerError, RateLimited, classify_status
from optbt.data.models import Candle, Contract, Expiries, Kind, from_epoch
from optbt.data.source import SourceError
from venues.instruments import OPTION_SERIES

log = logging.getLogger(__name__)

#: Under 200 a minute with room to spare, since a retry also counts.
MIN_INTERVAL = 60 / 180

#: After a refusal for rate, before asking again.
AFTER_REFUSAL = 60.0

MAX_ATTEMPTS = 5

#: Longest a single request may take. A three-month page of one-minute bars comes
#: back in well under a second; a minute means the connection is gone.
REQUEST_TIMEOUT = 60.0


def parse_strike(symbol: str, name: str) -> float | None:
    """The strike out of a contract symbol, given the underlying's short name.

    "NSE:NIFTY2661623500CE" and "NSE:NIFTY22OCT17500CE": after the name comes the
    expiry, and it is five characters in both spellings - YYMDD for a weekly
    ("26616", "26O06"), YYMON for a monthly ("22OCT"). What is left before CE/PE is
    the strike. None if the symbol does not have that shape, rather than a guess.
    """
    _, _, body = symbol.partition(":")
    if not body.startswith(name) or body[-2:] not in ("CE", "PE"):
        return None
    digits = body[len(name) + 5 : -2]
    try:
        return float(digits)
    except ValueError:
        return None


def _refusal(response: dict[str, Any]) -> type[BrokerError]:
    code = response.get("code")
    return classify_status(code if isinstance(code, int) else None, str(response.get("message")))


class FyersExpired:
    """The three endpoints, spaced and retried.

    Takes the SDK client rather than building one, so the token is whatever the
    caller already refreshed. `renew` builds a fresh one: a Fyers token dies at
    06:00 IST, and a four-year backfill started in the evening runs past it.
    """

    def __init__(
        self,
        client: Any,
        *,
        renew: Callable[[], Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        timeout: float | None = None,
    ) -> None:
        self._client = client
        self._renew = renew
        self._timeout = REQUEST_TIMEOUT if timeout is None else timeout
        self._sleep = sleep
        self._clock = clock
        self._last = -MIN_INTERVAL
        #: Seconds between requests. Raised while the market is open, when the
        #: desk spends the same budget on prices.
        self.interval = MIN_INTERVAL
        self.requests = 0

    def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            wait = self._last + self.interval - self._clock()
            if wait > 0:
                self._sleep(wait)
            self._last = self._clock()
            self.requests += 1
            try:
                response: dict[str, Any] = self._timed(method, params)
            except TimeoutError:
                # The SDK sets no timeout of its own: on 29 Sep a single request
                # sat on an open socket from 00:18 until morning and the whole run
                # with it. Abandon it, and ask again on a fresh connection.
                log.warning("%s took over %.0fs, attempt %d; reconnecting", method,
                            REQUEST_TIMEOUT, attempt)
                if self._renew is not None:
                    self._client = self._renew()
                continue
            except Exception as exc:  # the SDK raises bare exceptions on network faults
                log.warning("%s failed (%s), attempt %d", method, exc, attempt)
                self._sleep(AFTER_REFUSAL / 4)
                continue
            # "no_data" is an answer, not a refusal: a strike that was listed and
            # never traded. About a third of a weekly's far strikes are.
            if response.get("s") in ("ok", "no_data"):
                return response
            refusal = _refusal(response)
            if refusal is RateLimited:
                log.warning("rate limited on %s, waiting %.0fs", method, AFTER_REFUSAL)
                self._sleep(AFTER_REFUSAL)
                continue
            if refusal is AuthFailed and self._renew is not None:
                log.warning("token refused on %s; renewing it", method)
                self._client = self._renew()
                continue
            raise SourceError(
                f"{method} {params}: {response.get('message')} {response.get('data')}"
            )
        raise SourceError(f"{method} {params}: gave up after {MAX_ATTEMPTS} attempts")

    def _timed(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """The SDK call, given at most REQUEST_TIMEOUT to answer.

        On a worker thread, because the SDK offers no timeout to pass. A thread
        that never returns is left behind as a daemon - one per hung request,
        which is a small price against a backfill frozen for hours.
        """
        box: dict[str, Any] = {}

        def work() -> None:
            try:
                box["ok"] = getattr(self._client, method)(params)
            except BaseException as exc:
                box["err"] = exc

        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        worker.join(self._timeout)
        if worker.is_alive():
            raise TimeoutError(method)
        if "err" in box:
            raise box["err"]
        result: dict[str, Any] = box["ok"]
        return result

    def expiries(self, underlying: str, start: date, end: date) -> Expiries:
        response = self._call(
            "expiry_dates",
            {
                "symbol": OPTION_SERIES[underlying],
                "range_from": start.isoformat(),
                "range_to": end.isoformat(),
                "date_format": 1,
            },
        )
        found = response["data"]["expiry_dates"]
        return Expiries(
            options=tuple(sorted(date.fromisoformat(d) for d in found.get("options", []))),
            futures=tuple(sorted(date.fromisoformat(d) for d in found.get("futures", []))),
        )

    def live_expiries(self, underlying: str) -> list[tuple[date, bool]]:
        """Expiries still trading, nearest first, each with whether it is a monthly.

        From the live option chain: `expiry_dates` refuses any range that reaches
        today, so it cannot name an expiry that has not settled.
        """
        response = self._call(
            "optionchain",
            {"symbol": OPTION_SERIES[underlying], "strikecount": 1, "timestamp": ""},
        )
        found = response["data"].get("expiryData") or []
        out = [
            (datetime.strptime(e["date"], "%d-%m-%Y").date(), e.get("expiry_flag") == "M")
            for e in found
        ]
        return sorted(out)

    def contracts(self, underlying: str, expiry: date) -> list[Contract]:
        response = self._call(
            "history_underlying_symbols",
            {"symbol": OPTION_SERIES[underlying], "expiry_date": expiry.isoformat()},
        )
        data = response["data"]
        name = str(data.get("symbol", underlying))
        listed = data.get("contracts", {})
        out = [
            Contract(symbol=s, underlying=underlying, expiry=expiry, kind=Kind.FUTURE, strike=None)
            for s in dict.fromkeys(listed.get("futures", []))
        ]
        # Deduplicated: the listing for 30 Jun 2026 named NIFTY26JUN20000CE and
        # 22000CE twice each (581 symbols, 579 distinct), and a contract written
        # twice is refused by the ledger - which stopped a backfill outright.
        for symbol in dict.fromkeys(listed.get("options", [])):
            strike = parse_strike(symbol, name)
            if strike is None:
                log.warning("cannot read a strike from %s; skipped", symbol)
                continue
            out.append(
                Contract(
                    symbol=symbol,
                    underlying=underlying,
                    expiry=expiry,
                    kind=Kind(symbol[-2:]),
                    strike=strike,
                )
            )
        return out

    def candles(self, symbol: str, start: date, end: date) -> list[Candle]:
        response = self._call(
            "fno_historical_data",
            {
                "symbol": symbol,
                "resolution": "1",
                "date_format": 1,
                "range_from": start.isoformat(),
                "range_to": end.isoformat(),
                "include_oi": 1,
            },
        )
        return [_candle(row) for row in response.get("candles") or []]

    def index_candles(
        self, symbol: str, resolution: str, start: date, end: date
    ) -> list[Candle]:
        response = self._call(
            "history",
            {
                "symbol": symbol,
                "resolution": resolution,
                "date_format": "1",
                "range_from": start.isoformat(),
                "range_to": end.isoformat(),
                "cont_flag": "1",
            },
        )
        return [_candle(row) for row in response.get("candles") or []]


def _candle(row: list[Any]) -> Candle:
    return Candle(
        ts=from_epoch(int(row[0])),
        open=float(row[1]),
        high=float(row[2]),
        low=float(row[3]),
        close=float(row[4]),
        volume=int(row[5] or 0),
        oi=int(row[6] or 0) if len(row) > 6 else 0,
    )
