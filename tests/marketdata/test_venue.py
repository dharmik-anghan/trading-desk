"""The broker-history bar source."""

from __future__ import annotations

from datetime import UTC, date, datetime

from broker.models import Candle
from marketdata.models import Interval
from marketdata.venue import VenueBars, to_bar, weekday_bars


def _candle(ts: datetime, close: float = 100.0) -> Candle:
    return Candle(timestamp=ts, open=close, high=close, low=close, close=close, volume=1.0)


class _Broker:
    def __init__(self, candles: list[Candle]) -> None:
        self._candles = candles

    def get_history(self, symbol: str, resolution: str, start: date, end: date) -> list[Candle]:
        return self._candles


# As the store holds Fyers' daily bars: midnight UTC on the session's day.
FRIDAY = datetime(2026, 10, 9, tzinfo=UTC)
SATURDAY = datetime(2026, 10, 10, tzinfo=UTC)
MONDAY = datetime(2026, 10, 12, tzinfo=UTC)


def test_weekday_bars_drops_a_saturday_mock_session_by_its_ist_day() -> None:
    bars = [to_bar(_candle(t)) for t in (FRIDAY, SATURDAY, MONDAY)]
    assert [b.ts for b in weekday_bars(bars)] == [FRIDAY, MONDAY]


def test_an_nse_venue_never_returns_a_weekend_bar() -> None:
    broker = _Broker([_candle(SATURDAY, 24281.7), _candle(FRIDAY)])
    got = VenueBars(lambda: broker, weekdays_only=True).fetch("NSE:NIFTY50-INDEX", Interval.D1, 5)  # type: ignore[arg-type,return-value]
    assert [b.ts for b in got.bars] == [FRIDAY]


def test_a_venue_that_never_closes_keeps_its_weekends() -> None:
    broker = _Broker([_candle(SATURDAY)])
    got = VenueBars(lambda: broker).fetch("BTCUSDT", Interval.D1, 5)  # type: ignore[arg-type,return-value]
    assert len(got.bars) == 1
