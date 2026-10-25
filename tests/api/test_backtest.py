"""Running a built strategy through the API.

Wired to a store of its own, so these never read the real history: a test whose
result depends on what happened to Bitcoin last Tuesday is not a test.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.app import app
from marketdata import BarService, BarStore, Interval, Series
from marketdata.models import Bar

START = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def stocked() -> Iterator[TestClient]:
    """A client whose bar store holds one known, rising series."""
    store = BarStore()
    # A saw-tooth: rises for twenty bars, falls for ten, repeatedly. Enough for a
    # moving-average crossover to fire without being a straight line.
    bars: list[Bar] = []
    price = 100.0
    for i in range(1200):
        price += 1.0 if i % 30 < 20 else -1.6
        bars.append(
            Bar(
                ts=START + timedelta(minutes=5 * i),
                open=price,
                high=price + 0.5,
                low=price - 0.5,
                close=price,
                volume=1.0,
            )
        )
    store.write(Series("binance", "TESTUSDT", Interval.M5), bars)
    store.write_funding("binance", "TESTUSDT", [(START + timedelta(hours=8), 0.0001)])

    # Overridden inside the context, not before it: entering the client runs the
    # app's lifespan, which opens the real bar store and would replace anything
    # set beforehand - and on a machine where the desk is running it cannot open
    # that file at all, so the endpoint would answer 503 instead of using this.
    with TestClient(app) as client:
        previous_service = getattr(app.state, "bar_service", None)
        previous_store = getattr(app.state, "bar_store", None)
        app.state.bar_service = BarService(
            store, now=lambda: START + timedelta(minutes=5 * 1200)
        )
        app.state.bar_store = store
        try:
            yield client
        finally:
            app.state.bar_service = previous_service
            app.state.bar_store = previous_store
    store.close()


def _crossover() -> dict[str, Any]:
    fast = {"kind": "indicator", "name": "ema", "length": 5}
    slow = {"kind": "indicator", "name": "ema", "length": 20}
    return {
        "spec": {
            "name": "Test crossover",
            "long_entry": {"all": [{"left": fast, "op": "crosses_above", "right": slow}]},
            "long_exit": {"all": [{"left": fast, "op": "crosses_below", "right": slow}]},
        },
        "source": "binance",
        "symbol": "TESTUSDT",
        "interval": "5m",
        "days": 10,
        "capital": 1000.0,
        "leverage": 1.0,
        "slippage_bps": 1.0,
        "maker_entry": False,
    }


def test_a_run_returns_a_result_that_says_what_produced_it(stocked: TestClient) -> None:
    """A result that cannot be read back to its strategy is one nobody can check."""
    response = stocked.post("/api/backtest/run", json=_crossover())

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Test crossover"
    assert "EMA 5 crosses above EMA 20" in body["reads"]
    assert body["metrics"]["trades"] > 0
    assert len(body["curve"]) > 2


def test_every_result_carries_what_holding_would_have_done(stocked: TestClient) -> None:
    body = stocked.post("/api/backtest/run", json=_crossover()).json()

    assert "buy_and_hold" in body["metrics"]
    assert isinstance(body["metrics"]["beat_holding"], bool)


def test_a_longer_bar_size_is_built_from_what_is_stored(stocked: TestClient) -> None:
    """Only 5m is held, so an hourly run has to resample rather than fail."""
    request = {**_crossover(), "interval": "1h"}

    body = stocked.post("/api/backtest/run", json=request).json()

    assert body["interval"] == "1h"
    # 1,200 five-minute bars inside ten days is 100 hours
    assert body["bars"] == pytest.approx(100, abs=2)


def test_a_broken_strategy_says_what_to_change(stocked: TestClient) -> None:
    """The builder's own message, not a stack trace."""
    request = {**_crossover()}
    request["spec"] = {
        "name": "Nonsense",
        "long_entry": {
            "all": [
                {
                    "left": {"kind": "indicator", "name": "stochastic", "length": 9},
                    "op": "above",
                    "right": 0,
                }
            ]
        },
        "long_exit": {
            "all": [{"left": {"kind": "price", "field": "close"}, "op": "below", "right": 0}]
        },
    }

    response = stocked.post("/api/backtest/run", json=request)

    assert response.status_code == 400
    assert "no indicator called" in response.json()["detail"]


def test_a_strategy_with_no_entry_is_refused(stocked: TestClient) -> None:
    request = {**_crossover(), "spec": {"name": "Empty"}}

    response = stocked.post("/api/backtest/run", json=request)

    assert response.status_code == 400
    assert "Nothing to enter on" in response.json()["detail"]


def test_a_symbol_with_no_history_says_how_to_get_some(stocked: TestClient) -> None:
    request = {**_crossover(), "symbol": "NOTHINGUSDT"}

    response = stocked.post("/api/backtest/run", json=request)

    assert response.status_code == 404
    assert "backfill_bars.py" in response.json()["detail"]


def test_funding_from_another_source_is_labelled_as_a_proxy(stocked: TestClient) -> None:
    """The venue publishes none of its own, and a result must not hide that."""
    body = stocked.post("/api/backtest/run", json=_crossover()).json()

    assert any("proxy" in c for c in body["caveats"])


def test_the_curve_is_thinned_but_keeps_its_lowest_points(stocked: TestClient) -> None:
    """A curve thinned by sampling loses exactly the spikes a drawdown is made of."""
    body = stocked.post("/api/backtest/run", json=_crossover()).json()

    # A drawdown is ordered: a low before a peak is not a fall from it. Comparing
    # the global minimum with the global maximum would overstate it, which is the
    # mistake this is checking the thinning does not make.
    drawn = 0.0
    peak = float("-inf")
    for _, value in body["curve"]:
        peak = max(peak, value)
        drawn = max(drawn, (peak - value) / peak)

    # The curve is rounded to the paisa, so it can differ in the last decimal.
    assert body["metrics"]["max_drawdown"] == pytest.approx(drawn, abs=1e-4)
    assert drawn > 0


def test_leverage_beyond_the_venues_maximum_is_refused(stocked: TestClient) -> None:
    response = stocked.post("/api/backtest/run", json={**_crossover(), "leverage": 500})

    assert response.status_code == 422


def test_a_session_filter_is_applied_and_read_back(stocked: TestClient) -> None:
    """A result has to say which hours it was allowed to trade in."""
    request = {**_crossover()}
    request["spec"] = {**request["spec"], "sessions": ["london"]}

    body = stocked.post("/api/backtest/run", json=request).json()

    assert "Only during London 08:00-16:30 Europe/London" in body["reads"]


def test_an_unknown_session_is_refused_with_the_list(stocked: TestClient) -> None:
    request = {**_crossover()}
    request["spec"] = {**request["spec"], "sessions": ["atlantis"]}

    response = stocked.post("/api/backtest/run", json=request)

    assert response.status_code == 400
    assert "not a session" in response.json()["detail"]


def test_restricting_the_hours_changes_the_trades(stocked: TestClient) -> None:
    everywhere = stocked.post("/api/backtest/run", json=_crossover()).json()

    request = {**_crossover()}
    request["spec"] = {**request["spec"], "sessions": ["london"]}
    restricted = stocked.post("/api/backtest/run", json=request).json()

    assert restricted["metrics"]["trades"] < everywhere["metrics"]["trades"]


def test_candles_over_a_window(stocked: TestClient) -> None:
    """What a single trade is looked at on."""
    response = stocked.post(
        "/api/backtest/candles",
        json={
            "source": "binance",
            "symbol": "TESTUSDT",
            "interval": "5m",
            "start": "2026-01-01T00:00:00+00:00",
            "end": "2026-01-01T02:00:00+00:00",
        },
    )

    assert response.status_code == 200
    candles = response.json()["candles"]
    # two hours of five-minute bars, inclusive of both ends
    assert len(candles) == 25
    assert candles[0]["at"].startswith("2026-01-01T00:00")


def test_a_window_can_be_asked_for_at_a_longer_bar_size(stocked: TestClient) -> None:
    """Only 5m is stored, so an hourly window has to be resampled."""
    response = stocked.post(
        "/api/backtest/candles",
        json={
            "source": "binance",
            "symbol": "TESTUSDT",
            "interval": "1h",
            "start": "2026-01-01T00:00:00+00:00",
            "end": "2026-01-01T05:00:00+00:00",
        },
    )

    assert [c["at"][11:16] for c in response.json()["candles"]] == [
        "00:00", "01:00", "02:00", "03:00", "04:00", "05:00",
    ]


def test_a_backwards_window_is_refused(stocked: TestClient) -> None:
    response = stocked.post(
        "/api/backtest/candles",
        json={
            "source": "binance",
            "symbol": "TESTUSDT",
            "interval": "5m",
            "start": "2026-01-02T00:00:00+00:00",
            "end": "2026-01-01T00:00:00+00:00",
        },
    )

    assert response.status_code == 400
    assert "ends before it begins" in response.json()["detail"]


def test_a_window_that_is_not_a_timestamp_says_so(stocked: TestClient) -> None:
    response = stocked.post(
        "/api/backtest/candles",
        json={
            "source": "binance",
            "symbol": "TESTUSDT",
            "interval": "5m",
            "start": "yesterday",
            "end": "2026-01-01T00:00:00+00:00",
        },
    )

    assert response.status_code == 400
    assert "ISO timestamps" in response.json()["detail"]


def test_the_window_covers_the_trades_a_run_reported(stocked: TestClient) -> None:
    """The point of the endpoint: a trade in the log must be drawable.

    A run and a chart that disagree about what bars exist would make the picture
    beside a number meaningless.
    """
    run_body = stocked.post("/api/backtest/run", json=_crossover()).json()
    trade = run_body["trades"][0]

    response = stocked.post(
        "/api/backtest/candles",
        json={
            "source": "binance",
            "symbol": "TESTUSDT",
            "interval": run_body["interval"],
            "start": trade["opened_at"],
            "end": trade["closed_at"],
        },
    )

    assert response.status_code == 200
    assert response.json()["candles"]


def test_the_strategys_own_indicators_come_back_with_the_candles(
    stocked: TestClient,
) -> None:
    """A crossover chart without its two lines cannot show the crossing."""
    body = stocked.post(
        "/api/backtest/candles",
        json={
            "source": "binance",
            "symbol": "TESTUSDT",
            "interval": "5m",
            "start": "2026-01-01T12:00:00+00:00",
            "end": "2026-01-01T16:00:00+00:00",
            "spec": _crossover()["spec"],
        },
    ).json()

    labels = [line["label"] for line in body["lines"]]
    assert labels == ["EMA 20", "EMA 5"]  # slowest first, so it draws underneath
    for line in body["lines"]:
        assert len(line["values"]) == len(body["candles"])
        assert any(v is not None for v in line["values"])


def test_a_drawn_indicator_is_the_one_the_rule_read(stocked: TestClient) -> None:
    """The point of computing these in the engine's own view.

    A line drawn by different code from the one that made the decision can be
    subtly wrong exactly where it matters, at the crossing. Here the drawn value
    is checked against the indicator computed straight from the same bars.
    """
    from analytics.indicators import closes as close_prices
    from analytics.indicators import ema as ema_line
    from marketdata import Interval, Series

    store = app.state.bar_store
    bars = store.read(Series("binance", "TESTUSDT", Interval.M5))

    body = stocked.post(
        "/api/backtest/candles",
        json={
            "source": "binance",
            "symbol": "TESTUSDT",
            "interval": "5m",
            "start": bars[900].ts.isoformat(),
            "end": bars[910].ts.isoformat(),
            "spec": _crossover()["spec"],
        },
    ).json()

    drawn = next(line for line in body["lines"] if line["label"] == "EMA 5")
    expected = ema_line(close_prices(bars), 5)

    for offset, value in enumerate(drawn["values"]):
        assert value == pytest.approx(expected[900 + offset], rel=1e-9)


def test_a_higher_timeframe_line_holds_its_last_closed_value(
    stocked: TestClient,
) -> None:
    """Drawing the true hourly value against a five-minute bar inside that hour
    would draw information the rule did not have - the same lie the engine exists
    to avoid, just in pixels."""
    spec = {
        "name": "Hourly filter",
        "long_entry": {
            "all": [
                {
                    "left": {"kind": "indicator", "name": "ema", "length": 5, "tf": "1h"},
                    "op": "above",
                    "right": 0,
                }
            ]
        },
        "long_exit": {
            "all": [{"left": {"kind": "price", "field": "close"}, "op": "below", "right": 0}]
        },
    }

    body = stocked.post(
        "/api/backtest/candles",
        json={
            "source": "binance",
            "symbol": "TESTUSDT",
            "interval": "5m",
            "start": "2026-01-02T00:00:00+00:00",
            "end": "2026-01-02T02:00:00+00:00",
            "spec": spec,
        },
    ).json()

    values = [v for v in body["lines"][0]["values"] if v is not None]

    assert body["lines"][0]["label"] == "EMA 5 1h"
    # twenty-five five-minute bars across two hours, but only three distinct
    # hourly values can have closed in that time
    assert len(values) > 12
    assert len(set(values)) <= 3


def test_a_chart_is_still_drawn_for_a_strategy_that_will_not_run(
    stocked: TestClient,
) -> None:
    """A broken strategy is a reason to show no lines, not to show no chart."""
    body = stocked.post(
        "/api/backtest/candles",
        json={
            "source": "binance",
            "symbol": "TESTUSDT",
            "interval": "5m",
            "start": "2026-01-01T00:00:00+00:00",
            "end": "2026-01-01T02:00:00+00:00",
            "spec": {"name": "Broken"},
        },
    ).json()

    assert body["candles"]
    assert body["lines"] == []


def test_a_pivot_gap_percentile_can_be_traded_on(stocked: TestClient) -> None:
    """The squeeze reading: enter when the previous day's range was among the
    narrowest of the window."""
    request = {**_crossover(), "interval": "1h"}
    request["spec"] = {
        "name": "Squeeze",
        "long_entry": {
            "all": [
                {
                    "left": {
                        "kind": "indicator",
                        "name": "pivot_gap_rank",
                        "length": 20,
                        "tf": "1d",
                    },
                    "op": "below",
                    "right": 20,
                }
            ]
        },
        "long_exit": {
            "all": [{"left": {"kind": "price", "field": "close"}, "op": "below", "right": 0}]
        },
    }

    body = stocked.post("/api/backtest/run", json=request).json()

    assert "pivot gap percentile over 20 1d below 20" in body["reads"]


def test_a_pivot_gap_line_is_not_drawn_on_the_price_axis(stocked: TestClient) -> None:
    """A percentile running 0 to 100 on a chart scaled to Bitcoin would sit on the
    floor and flatten every candle above it."""
    spec = {
        "name": "Squeeze",
        "long_entry": {
            "all": [
                {
                    "left": {"kind": "indicator", "name": "pivot_gap_rank", "length": 5},
                    "op": "below",
                    "right": 20,
                },
                {
                    "left": {"kind": "indicator", "name": "ema", "length": 5},
                    "op": "above",
                    "right": 0,
                },
            ]
        },
        "long_exit": {
            "all": [{"left": {"kind": "price", "field": "close"}, "op": "below", "right": 0}]
        },
    }

    body = stocked.post(
        "/api/backtest/candles",
        json={
            "source": "binance",
            "symbol": "TESTUSDT",
            "interval": "5m",
            "start": "2026-01-01T12:00:00+00:00",
            "end": "2026-01-01T18:00:00+00:00",
            "spec": spec,
        },
    ).json()

    on_price = {line["label"]: line["on_price"] for line in body["lines"]}

    assert on_price["EMA 5"] is True
    assert on_price["Pivot gap percentile 5"] is False


def test_pivot_gap_takes_no_period(stocked: TestClient) -> None:
    """It averages nothing, so a period of zero is not a mistake to refuse."""
    request = {**_crossover()}
    request["spec"] = {
        "name": "Gap",
        "long_entry": {
            "all": [
                {
                    "left": {"kind": "indicator", "name": "pivot_gap", "length": 0},
                    "op": "above",
                    "right": 5,
                }
            ]
        },
        "long_exit": {
            "all": [{"left": {"kind": "price", "field": "close"}, "op": "below", "right": 0}]
        },
    }

    response = stocked.post("/api/backtest/run", json=request)

    assert response.status_code == 200
    assert "pivot gap % above 5" in response.json()["reads"]


def test_a_strategy_can_be_checked_against_random_entries(
    stocked: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check answers with a verdict and the numbers it was reached from."""
    monkeypatch.setattr("os.cpu_count", lambda: 1)

    response = stocked.post("/api/backtest/check", json={**_crossover(), "random_runs": 20})

    assert response.status_code == 200
    body = response.json()
    assert body["verdict"] in ("pass", "weak", "fail")
    assert len(body["checks"]) == 4
    assert body["random_runs"] == 20
    assert 0 < body["p_value"] <= 1
