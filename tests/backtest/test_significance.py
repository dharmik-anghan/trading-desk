"""The Sharpe ratio's honesty checks, against numbers worked out independently."""

from __future__ import annotations

import random

import pytest

from backtest.significance import (
    deflated_sharpe,
    expected_max_sharpe,
    moments,
    probabilistic_sharpe,
    sharpe,
)


def test_sharpe_is_mean_over_sample_deviation() -> None:
    # mean 0.02; sample variance is (0.01^2 + 0.01^2) / (2 - 1).
    assert sharpe([0.01, 0.03]) == pytest.approx(0.02 / (2 * 0.01**2) ** 0.5)
    assert sharpe([0.01]) == 0.0
    assert sharpe([0.02, 0.02, 0.02]) == 0.0


def test_a_normal_samples_moments_are_close_to_a_normals() -> None:
    rng = random.Random(1)
    skew, kurtosis = moments([rng.gauss(0, 1) for _ in range(50_000)])
    assert skew == pytest.approx(0, abs=0.05)
    assert kurtosis == pytest.approx(3, abs=0.1)


def test_probabilistic_sharpe_by_hand() -> None:
    """Bailey and Lopez de Prado's formula with the moments of a normal.

    With skew 0 and kurtosis 3 the denominator is sqrt(1 + SR^2 / 2), so the result
    can be checked without the function under test.
    """
    from statistics import NormalDist

    rng = random.Random(3)
    returns = [rng.gauss(0.001, 0.01) for _ in range(500)]
    observed = sharpe(returns)
    skew, kurtosis = moments(returns)
    z = observed * (499) ** 0.5 / (1 - skew * observed + (kurtosis - 1) / 4 * observed**2) ** 0.5

    assert probabilistic_sharpe(returns) == pytest.approx(NormalDist().cdf(z))


def test_a_clear_edge_is_probably_real_and_noise_is_not() -> None:
    rng = random.Random(5)
    edge = [rng.gauss(0.004, 0.01) for _ in range(400)]
    assert probabilistic_sharpe(edge) > 0.99

    # Any one noise series can land anywhere; across many they centre on a coin flip.
    noise = [
        probabilistic_sharpe([rng.gauss(0.0, 0.01) for _ in range(400)]) for _ in range(100)
    ]
    assert 0.4 < sum(noise) / len(noise) < 0.6


def test_more_trials_raise_the_bar_the_best_one_has_to_clear() -> None:
    rng = random.Random(7)
    few = [rng.gauss(0, 0.1) for _ in range(5)]
    many = [rng.gauss(0, 0.1) for _ in range(500)]

    assert expected_max_sharpe(many) > expected_max_sharpe(few) > 0
    assert expected_max_sharpe([0.3]) == 0.0
    assert expected_max_sharpe([0.2, 0.2, 0.2]) == 0.0


def test_the_best_of_many_noise_strategies_is_not_significant_once_deflated() -> None:
    """The point of the whole exercise, as an experiment.

    200 strategies with no edge. The best looks fine by its own Sharpe; deflated by
    the number of tries it should not.
    """
    rng = random.Random(11)
    runs = [[rng.gauss(0, 0.01) for _ in range(250)] for _ in range(200)]
    sharpes = [sharpe(r) for r in runs]
    best = runs[max(range(200), key=lambda i: sharpes[i])]

    assert probabilistic_sharpe(best) > 0.95, "looks significant on its own"
    assert deflated_sharpe(best, sharpes) < 0.8, "and is not, after 200 tries"


def test_a_real_edge_survives_deflation() -> None:
    rng = random.Random(13)
    runs = [[rng.gauss(0, 0.01) for _ in range(250)] for _ in range(50)]
    sharpes = [sharpe(r) for r in runs]
    real = [rng.gauss(0.006, 0.01) for _ in range(250)]

    assert deflated_sharpe(real, [*sharpes, sharpe(real)]) > 0.95
