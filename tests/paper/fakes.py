"""A Fyers stand-in for the NSE market: real symbol shapes, whole-lot figures."""

from __future__ import annotations

from datetime import UTC, datetime

from broker.models import Expiry, OptionChain, OptionChainRow, Quote

SPOT = 25010.0
LOT = 65
WEEKLY = Expiry(date="06-10-2026", token="w1", weekly=True)
MONTHLY = Expiry(date="27-10-2026", token="m1", weekly=False)


def symbol(strike: float, kind: str, expiry: Expiry = WEEKLY) -> str:
    code = "26O06" if expiry is WEEKLY else "26OCT"
    return f"NSE:NIFTY{code}{int(strike)}{kind}"


class FakeFyers:
    """Strikes every 50 around 25,000; a call is worth its distance below the
    money plus time value, and the book is a rupee wide either side."""

    def __init__(self, spot: float = SPOT) -> None:
        self.spot = spot
        self.chains_read = 0

    def _premium(self, strike: float, kind: str) -> float:
        intrinsic = max(self.spot - strike, 0) if kind == "CE" else max(strike - self.spot, 0)
        return round(intrinsic + max(5.0, 120 - abs(strike - self.spot) * 0.4), 2)

    def _rows(self, expiry: Expiry) -> list[OptionChainRow]:
        rows = []
        for i in range(-12, 13):
            strike = 25000 + 50 * i
            for kind in ("CE", "PE"):
                p = self._premium(strike, kind)
                rows.append(
                    OptionChainRow(
                        symbol=symbol(strike, kind, expiry),
                        strike=strike,
                        option_type=kind,
                        ltp=p,
                        bid=p - 1,
                        ask=p + 1,
                        oi=LOT * (100 + i * i),
                        prev_oi=0,
                        volume=LOT * 40,
                    )
                )
        return rows

    def get_option_chain(
        self, symbol: str, strike_count: int = 10, expiry_token: str = ""
    ) -> OptionChain:
        self.chains_read += 1
        expiry = MONTHLY if expiry_token == MONTHLY.token else WEEKLY
        return OptionChain(
            underlying_symbol=symbol,
            underlying_ltp=self.spot,
            fetched_at=datetime.now(UTC),
            rows=self._rows(expiry),
            expiries=[WEEKLY, MONTHLY],
            expiry_token=expiry.token,
        )

    def get_quote(self, symbols: list[str]) -> dict[str, Quote]:
        by = {r.symbol: r for e in (WEEKLY, MONTHLY) for r in self._rows(e)}
        now = datetime.now(UTC)
        out = {}
        for s in symbols:
            r = by.get(s)
            price = r.ltp if r else self.spot
            out[s] = Quote(
                symbol=s,
                ltp=price,
                open=price,
                high=price,
                low=price,
                prev_close=price,
                volume=0,
                bid=r.bid if r else price,
                ask=r.ask if r else price,
                timestamp=now,
            )
        return out
