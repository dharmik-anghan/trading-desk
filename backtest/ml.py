"""A model that decides which of a rule's signals to take.

This is meta-labeling. The rule keeps its job - direction and timing - and the model
is asked one question about each signal the rule raises: will this one make money?
It cannot invent a trade the rule would not take, cannot flip a side, and cannot
touch an exit, so the worst it can do is skip signals it should have taken. That is
the safest place to put a model in a strategy, and the reason it is built this way
rather than as a model that predicts price.

How a model is built and judged here:

**Labels come from the engine.** The base rule is run over the train window through
`engine.run`, with a recorder in front of it that notes the feature vector at every
signal. Each trade the engine then produced is matched to the signal that made it
and labelled by what it earned after costs. Nothing is simulated a second way.

**Cross-validation is forward, purged and embargoed.** Trades have a duration: one
entered at 10:00 and closed at 14:00 has a label that was not known until 14:00. A
model trained on it and asked about a signal at 12:00 has seen the future. So when
a block of trades is held out, every training trade that was still open when the
block began - and every one within an embargo before it - is dropped. Training is
only ever on blocks earlier than the one being predicted.

**The threshold is chosen on the train window alone.** From the out-of-fold
probabilities, the cut that earns the most per trade while keeping a fixed share of
signals. If no cut beats taking everything, the threshold is zero and the filter is
a no-op - a model with nothing to say does not get to say it.

**It is judged by running the engine again.** The test window is traded with the
rule behind a `FilteredRule`, so a skipped signal changes which signal is taken
next, what it costs and where the position is - effects a count of "trades avoided"
would never see.
"""

from __future__ import annotations

import logging
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import joblib
import lightgbm as lgb
import numpy as np

from backtest import features
from backtest.engine import Rule
from backtest.market import Side
from backtest.models import Action, Intent, Position
from backtest.study import Study, Window
from backtest.view import View
from backtest.walkforward import FitFilter, Fitted
from marketdata.models import Interval

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MLConfig:
    #: Fewer labelled trades than this and there is nothing to learn from.
    min_samples: int = 150
    #: Forward-chaining folds inside the train window, for out-of-fold probabilities.
    inner_splits: int = 4
    #: Bars before a held-out block that no training trade may reach into.
    embargo_bars: int = 12
    #: The filter must keep at least this share of signals.
    min_keep: float = 0.25
    trees: int = 150
    #: Where fitted models are written, with their features and train window.
    save_dir: Path | None = None
    workers: int = 4


@dataclass(frozen=True)
class Sample:
    """One signal that became a trade, and how the trade went."""

    decided_at: datetime
    closed_at: datetime
    features: tuple[float, ...]
    #: Net return on the position's value, so trades are comparable whatever the
    #: account was worth when they were taken.
    result: float

    @property
    def won(self) -> bool:
        return self.result > 0


# ---------------------------------------------------------------------------
# Recording signals
# ---------------------------------------------------------------------------


@dataclass
class Signal:
    at: datetime
    side: Side
    features: list[float]


class Recorder:
    """Passes a rule through unchanged, noting the vector at each entry it raises."""

    def __init__(self, inner: Rule, interval: Interval, sink: list[Signal]) -> None:
        self.inner = inner
        self.name = inner.name
        self.context = _widened(inner.context, interval)
        self._sink = sink

    def entry(self, view: View) -> Intent:
        intent = self.inner.entry(view)
        if intent.action is Action.ENTER and intent.side is not None and view.at is not None:
            self._sink.append(Signal(view.at, intent.side, features.extract(view, intent.side)))
        return intent

    def exit(self, view: View, position: Position) -> Intent:
        return self.inner.exit(view, position)


def _widened(context: tuple[Interval, ...], interval: Interval) -> tuple[Interval, ...]:
    """The rule's own timeframes plus the ones the vector reads."""
    return tuple(
        sorted({*context, *features.context(interval)}, key=lambda i: i.seconds, reverse=True)
    )


def collect(
    study: Study, raw: Mapping[str, Any], values: Mapping[str, Any], window: Window
) -> tuple[list[Sample], tuple[str, ...]]:
    """Run the base rule over `window` and label each trade it took."""
    signals: list[Signal] = []
    interval = study.interval
    done = study.run(
        raw, values, window, wrap=lambda rule: Recorder(rule, interval, signals)
    )
    times = [s.at for s in signals]
    samples: list[Sample] = []
    for trade in done.result.trades:
        # The signal behind a trade is the last one raised no later than the
        # trade's fill, on the same side. For a fill at the next open that is the
        # bar before; for a resting order it may be several bars before, and a
        # newer setup replaces an older one, so the latest is the one that filled.
        at = bisect_right(times, trade.opened_at) - 1
        while at >= 0 and signals[at].side is not trade.side:
            at -= 1
        if at < 0 or trade.quantity <= 0 or trade.entry <= 0:
            continue
        samples.append(
            Sample(
                decided_at=signals[at].at,
                closed_at=trade.closed_at,
                features=tuple(signals[at].features),
                result=trade.net / (trade.entry * trade.quantity),
            )
        )
    return samples, features.names(interval)


# ---------------------------------------------------------------------------
# Validation without leakage
# ---------------------------------------------------------------------------


def purged_splits(
    samples: Sequence[Sample], splits: int, embargo: timedelta
) -> list[tuple[list[int], list[int]]]:
    """Forward-chaining (train, validate) index pairs with leaking trades removed.

    The samples are cut into `splits + 1` blocks by decision time. Block k is
    validated against a model trained on blocks before it - never after - and a
    training trade is dropped if it had not closed `embargo` before the block's
    first decision, because its label then depends on what happened in the block.
    """
    order = sorted(range(len(samples)), key=lambda i: samples[i].decided_at)
    blocks = [list(b) for b in np.array_split(np.array(order), splits + 1) if len(b)]
    out: list[tuple[list[int], list[int]]] = []
    for k in range(1, len(blocks)):
        valid = blocks[k]
        starts = min(samples[i].decided_at for i in valid)
        train = [
            i for block in blocks[:k] for i in block if samples[i].closed_at < starts - embargo
        ]
        out.append((train, valid))
    return out


def pick_threshold(probability: np.ndarray, result: np.ndarray, min_keep: float) -> float:
    """The probability cut that earns the most while keeping `min_keep` of the signals.

    Zero - take everything - unless some cut beats taking everything. The
    comparison is on summed return per trade, the thing the strategy is paid in.
    """
    if len(probability) == 0:
        return 0.0
    best_cut, best_total = 0.0, float(result.sum())
    for keep in np.arange(1.0, min_keep - 1e-9, -0.05):
        cut = float(np.quantile(probability, 1.0 - keep))
        total = float(result[probability >= cut].sum())
        if total > best_total:
            best_cut, best_total = cut, total
    return best_cut


# ---------------------------------------------------------------------------
# Fitting
# ---------------------------------------------------------------------------


@dataclass
class Model:
    booster: lgb.LGBMClassifier
    names: tuple[str, ...]
    threshold: float
    importance: dict[str, float]
    #: What the out-of-fold predictions say the filter would have done in training.
    kept: float
    gain: float

    def probability(self, row: Sequence[float]) -> float:
        return float(np.asarray(self.booster.predict_proba(np.array([row])))[0, 1])


def _classifier(cfg: MLConfig, samples: int, seed: int) -> lgb.LGBMClassifier:
    return lgb.LGBMClassifier(
        n_estimators=cfg.trees,
        learning_rate=0.05,
        num_leaves=15,
        max_depth=4,
        min_child_samples=max(20, samples // 40),
        subsample=0.8,
        subsample_freq=1,
        colsample_bytree=0.8,
        reg_lambda=5.0,
        random_state=seed,
        n_jobs=cfg.workers,
        deterministic=True,
        force_row_wise=True,
        verbose=-1,
    )


def fit(
    samples: Sequence[Sample],
    names: tuple[str, ...],
    cfg: MLConfig,
    interval: Interval,
    seed: int = 1,
) -> Model | None:
    """Fit on `samples`, choose the threshold out-of-fold, and refit on all of them."""
    x = np.array([s.features for s in samples], dtype=float)
    y = np.array([s.won for s in samples], dtype=int)
    result = np.array([s.result for s in samples], dtype=float)
    if len(samples) < cfg.min_samples or y.min() == y.max():
        return None

    embargo = timedelta(seconds=cfg.embargo_bars * interval.seconds)
    held: list[int] = []
    predicted: list[float] = []
    for train, valid in purged_splits(samples, cfg.inner_splits, embargo):
        if len(train) < 50 or y[train].min() == y[train].max():
            continue
        model = _classifier(cfg, len(train), seed).fit(x[train], y[train])
        held += valid
        predicted += list(np.asarray(model.predict_proba(x[valid]))[:, 1])

    oof, earned = np.array(predicted), result[held]
    threshold = pick_threshold(oof, earned, cfg.min_keep) if len(held) >= 50 else 0.0
    kept = float((oof >= threshold).mean()) if len(oof) else 1.0
    gain = float(earned[oof >= threshold].sum() - earned.sum()) if len(oof) else 0.0

    final = _classifier(cfg, len(samples), seed).fit(x, y)
    totals = final.booster_.feature_importance(importance_type="gain").astype(float)
    shares = totals / totals.sum() if totals.sum() > 0 else totals
    importance = dict(sorted(zip(names, shares, strict=True), key=lambda p: -p[1]))
    return Model(final, names, threshold, importance, kept, gain)


class FilteredRule:
    """A rule that may decline its own entries, and nothing else.

    `entry` asks the base rule and passes its answer through unless the model puts
    the probability of a profit below the threshold, in which case there is no
    entry. Exits, stops and targets are the base rule's and are untouched, and a
    filtered signal leaves the engine flat exactly as if the rule had not fired.
    """

    def __init__(self, base: Rule, model: Model, interval: Interval) -> None:
        self.base = base
        self.model = model
        self.name = f"{base.name} + filter"
        self.context = _widened(base.context, interval)
        #: Signals the base rule raised, and the ones declined.
        self.seen = 0
        self.vetoed = 0

    def entry(self, view: View) -> Intent:
        intent = self.base.entry(view)
        if intent.action is not Action.ENTER or intent.side is None:
            return intent
        self.seen += 1
        if self.model.threshold <= 0.0:
            return intent
        if self.model.probability(features.extract(view, intent.side)) < self.model.threshold:
            self.vetoed += 1
            return Intent.nothing()
        return intent

    def exit(self, view: View, position: Position) -> Intent:
        return self.base.exit(view, position)


# ---------------------------------------------------------------------------
# The hook walk-forward uses
# ---------------------------------------------------------------------------


@dataclass
class _Counter:
    fits: int = 0
    saved: list[Path] = field(default_factory=list)


def make_fit_filter(cfg: MLConfig, seed: int = 1) -> FitFilter:
    """A `FitFilter` for `walk_forward`: fit on the train window, return the wrapper."""
    counter = _Counter()

    def fit_filter(
        study: Study, raw: Mapping[str, Any], values: dict[str, Any], window: Window
    ) -> Fitted | None:
        samples, names = collect(study, raw, values, window)
        model = fit(samples, names, cfg, study.interval, seed + counter.fits)
        counter.fits += 1
        if model is None:
            log.info("entry filter: %d trades is too few to learn from", len(samples))
            return None

        bars = study.bars
        info: dict[str, Any] = {
            "samples": len(samples),
            "win_rate": sum(s.won for s in samples) / len(samples),
            "threshold": model.threshold,
            "kept_oof": model.kept,
            "gain_oof": model.gain,
            "importance": model.importance,
            "train": [bars[window.start].ts.isoformat(), bars[window.end - 1].ts.isoformat()],
        }
        if cfg.save_dir is not None:
            cfg.save_dir.mkdir(parents=True, exist_ok=True)
            path = cfg.save_dir / f"filter-{counter.fits:02d}.joblib"
            joblib.dump(
                {
                    "model": model.booster,
                    "features": model.names,
                    "threshold": model.threshold,
                    "train": info["train"],
                    "values": values,
                    "config": {**asdict(cfg), "save_dir": str(cfg.save_dir)},
                },
                path,
            )
            counter.saved.append(path)
            info["saved"] = str(path)

        def wrap(rule: Rule) -> Rule:
            return FilteredRule(rule, model, study.interval)

        return Fitted(wrap, info)

    return fit_filter
