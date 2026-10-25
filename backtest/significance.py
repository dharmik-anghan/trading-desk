"""How much a Sharpe ratio can be believed.

A search that tries four hundred parameter sets and keeps the best has not found a
Sharpe of 2; it has found the largest of four hundred draws, and the largest of
four hundred draws from a strategy with no edge at all is comfortably above zero.
The Deflated Sharpe Ratio (Bailey and Lopez de Prado, 2014) puts a number on that:
the probability that the observed Sharpe exceeds what the best of N trials would
have shown by luck alone, given how spread out the trials' Sharpes were and how
fat-tailed the returns are.

Everything is per-period (here, per day) rather than annualised. The formulas
count observations, and a daily series has the right number of roughly independent
ones; five-minute returns of a strategy that is flat most of the day do not.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from statistics import NormalDist

_NORMAL = NormalDist()
#: The Euler-Mascheroni constant, which the expected maximum of N normals uses.
_EULER = 0.5772156649015329


def sharpe(returns: Sequence[float]) -> float:
    """Mean over sample standard deviation. Zero when there is nothing to divide by."""
    n = len(returns)
    if n < 2:
        return 0.0
    mean = sum(returns) / n
    variance = sum((r - mean) ** 2 for r in returns) / (n - 1)
    return mean / math.sqrt(variance) if variance > 0 else 0.0


def moments(returns: Sequence[float]) -> tuple[float, float]:
    """Skewness and (non-excess) kurtosis of the returns. A normal's are 0 and 3."""
    n = len(returns)
    if n < 3:
        return 0.0, 3.0
    mean = sum(returns) / n
    m2 = sum((r - mean) ** 2 for r in returns) / n
    if m2 <= 0:
        return 0.0, 3.0
    m3 = sum((r - mean) ** 3 for r in returns) / n
    m4 = sum((r - mean) ** 4 for r in returns) / n
    return m3 / m2**1.5, m4 / m2**2


def probabilistic_sharpe(returns: Sequence[float], benchmark: float = 0.0) -> float:
    """The probability that the true Sharpe exceeds `benchmark`, per period.

    Corrects the usual standard error of a Sharpe for skew and fat tails: a
    strategy that makes small gains often and a large loss rarely has a Sharpe that
    is less certain than a normal's would be.
    """
    n = len(returns)
    if n < 3:
        return 0.5
    observed = sharpe(returns)
    skew, kurtosis = moments(returns)
    spread = 1.0 - skew * observed + (kurtosis - 1.0) / 4.0 * observed**2
    if spread <= 0:
        return 0.5
    z = (observed - benchmark) * math.sqrt(n - 1) / math.sqrt(spread)
    return _NORMAL.cdf(z)


def expected_max_sharpe(trial_sharpes: Sequence[float]) -> float:
    """The best Sharpe that N trials with no edge would be expected to show.

    From the spread of the Sharpes actually observed across the trials: a search
    whose trials all scored alike has less luck to deflate by than one whose trials
    ranged widely.
    """
    n = len(trial_sharpes)
    if n < 2:
        return 0.0
    mean = sum(trial_sharpes) / n
    variance = sum((s - mean) ** 2 for s in trial_sharpes) / (n - 1)
    # Not `<= 0`: trials that all scored alike differ by float rounding, and a
    # deviation of 1e-17 would still be multiplied by the same z-scores.
    if variance <= 1e-18:
        return 0.0
    return math.sqrt(variance) * (
        (1.0 - _EULER) * _NORMAL.inv_cdf(1.0 - 1.0 / n)
        + _EULER * _NORMAL.inv_cdf(1.0 - 1.0 / (n * math.e))
    )


def deflated_sharpe(returns: Sequence[float], trial_sharpes: Sequence[float]) -> float:
    """The probability that `returns`' Sharpe is real after trying `len(trial_sharpes)` things.

    Near 1 means the result survives the number of attempts that produced it; near
    0.5 or below means a Sharpe that size is what that many tries would give a
    strategy with nothing in it. Conventionally 0.95 is the bar.
    """
    return probabilistic_sharpe(returns, benchmark=expected_max_sharpe(trial_sharpes))
