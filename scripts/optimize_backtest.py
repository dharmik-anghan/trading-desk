"""Walk-forward parameter search on a rule-based strategy, from the command line.

    uv run python scripts/optimize_backtest.py scripts/specs/ema_cross.json \\
        --symbol BTCUSDT --interval 5m --trials 150
    uv run python scripts/optimize_backtest.py scripts/specs/rsi_reversion.json --ml

The spec is an ordinary backtest spec in which any number may be written as
`{"param": "name"}` and described in a `params` block - see `backtest/params.py`.
A parameter's `default` is the value a person would have typed, and is what
"hand-tuned" means in the output: the strategy before any search.

What it prints, in the order to read it:

  folds       each round of search-then-trade: the settings found on the train
              window, their score there and on the test window after, how stable
              they are against their neighbours, and the Deflated Sharpe
  results     the hand-tuned strategy in-sample, then hand-tuned, optimized (and
              optimized + ML filter, with --ml) on the out-of-sample windows and on
              the holdout that no search ever saw

Trades every trial through `backtest/engine.py`, the same engine the Backtesting
page runs, so a number here is a number there.

The bar store is a DuckDB file that one process may hold. When the desk is running
and holds it, this reads a copy taken to `logs/` rather than asking you to stop
the desk; that is fine for history, which only changes when a backfill runs.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import duckdb  # noqa: E402

import paths  # noqa: E402
from backtest.engine import Execution  # noqa: E402
from backtest.metrics import Metrics  # noqa: E402
from backtest.params import declared, defaults  # noqa: E402
from backtest.service import Floors, Series, prepare  # noqa: E402
from backtest.study import Study, Workers  # noqa: E402
from backtest.walkforward import (  # noqa: E402
    OBJECTIVES,
    FitFilter,
    Goal,
    Report,
    Variant,
    make_plan,
    walk_forward,
)
from marketdata import BarService, Interval  # noqa: E402
from marketdata.store import BarStore  # noqa: E402


def _open_store(path: Path) -> tuple[BarStore, str]:
    """The bar store, or a copy of it when another process holds the file."""
    try:
        return BarStore(path), ""
    except duckdb.IOException:
        paths.LOGS_DIR.mkdir(exist_ok=True)
        copy = paths.LOGS_DIR / "bars-snapshot.duckdb"
        shutil.copyfile(path, copy)
        wal = Path(f"{path}.wal")
        if wal.exists():
            shutil.copyfile(wal, Path(f"{copy}.wal"))
        return BarStore(copy), f"the desk holds {path.name}; read a copy at {copy}"


def _params(values: dict[str, Any]) -> str:
    return " ".join(f"{k}={v:g}" if isinstance(v, float) else f"{k}={v}" for k, v in values.items())


def _day(bar_time: datetime) -> str:
    return bar_time.strftime("%Y-%m-%d")


def _row(label: str, m: Metrics | None) -> str:
    if m is None:
        return f"  {label:<34} {'-':>6}"
    calmar = f"{m.calmar:>6.2f}" if m.calmar is not None else f"{'-':>6}"
    return (
        f"  {label:<34} {m.trades:>6} {m.total_return:>+8.1%} "
        f"{m.annualised if m.annualised is not None else float('nan'):>+8.1%} "
        f"{m.max_drawdown:>6.1%} {m.sharpe:>6.2f} {calmar} {m.win_rate:>6.1%} "
        f"{m.buy_and_hold:>+8.1%}"
    )


def _holdout_metrics(variant: Variant) -> Metrics | None:
    return variant.holdout.metrics if variant.holdout else None


def _print_folds(study: Study, report: Report) -> None:
    bars = study.bars
    print("\nFOLDS  (score is the chosen objective; stab = neighbours' mean / best)")
    print(
        f"  {'#':>2} {'train':<23} {'test':<23} {'settings':<44} "
        f"{'train':>6} {'test':>6} {'trd':>4} {'stab':>5} {'DSR':>5}"
    )
    for f in report.folds:
        fold = f.fold
        train = f"{_day(bars[fold.train.start].ts)}..{_day(bars[fold.train.end - 1].ts)}"
        test = f"{_day(bars[fold.test.start].ts)}..{_day(bars[fold.test.end - 1].ts)}"
        ratio = f.stability.ratio if f.stability else None
        flag = "!" if f.stability and f.stability.sharp else " "
        print(
            f"  {fold.number + 1:>2} {train:<23} {test:<23} {_params(f.values):<44} "
            f"{_fmt(f.train_value):>6} {_fmt(f.test_value):>6} {f.test.trades:>4} "
            f"{_fmt(ratio):>5}{flag}{_fmt(f.deflated):>5}"
            + (f"  [{f.note}]" if f.note else "")
        )
    spikes = sum(1 for f in report.folds if f.stability and f.stability.sharp)
    if spikes:
        print(f"  ! {spikes} of {len(report.folds)} folds chose a spike, not a plateau")


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def _print_results(report: Report) -> None:
    print("\nRESULTS")
    print(
        f"  {'':<34} {'trades':>6} {'net':>8} {'ann.':>8} {'maxDD':>6} "
        f"{'sharpe':>6} {'calmar':>6} {'win':>6} {'hold':>8}"
    )
    print(_row("hand-tuned   in-sample (research)", report.baseline_research.metrics))
    print()
    variants = [report.baseline, report.tuned, *report.others]
    for variant in variants:
        print(_row(f"{variant.name:<13} out-of-sample", variant.chain.metrics()))
    print()
    for variant in variants:
        print(_row(f"{variant.name:<13} HOLDOUT", _holdout_metrics(variant)))

    print(f"\n  P(out-of-sample Sharpe > 0): {report.oos_probability():.2f}")
    deflated = report.oos_deflated()
    if deflated is not None:
        print(f"  mean Deflated Sharpe of the fold winners (train): {deflated:.2f}")
    print(f"  settings for the holdout: {_params(report.final_values)}")


def _print_filters(report: Report) -> None:
    filters = report.extra.get("filters", [])
    if not filters:
        return
    print("\nENTRY FILTER  (fitted on each train window; 'oof' is out-of-fold, inside train)")
    print(f"  {'fold':>4} {'trades':>6} {'win%':>6} {'cut':>6} {'kept':>6} {'oof gain':>9}")
    for f in filters:
        label = "hold" if f["fold"] < 0 else str(f["fold"] + 1)
        print(
            f"  {label:>4} {f['samples']:>6} {f['win_rate']:>6.1%} {f['threshold']:>6.2f} "
            f"{f['kept_oof']:>6.1%} {f['gain_oof']:>+9.2f}"
        )
    totals: dict[str, float] = {}
    for f in filters:
        for name, share in f["importance"].items():
            totals[name] = totals.get(name, 0.0) + share / len(filters)
    top = sorted(totals.items(), key=lambda p: -p[1])[:8]
    print("  what the model keys on: " + ", ".join(f"{n} {v:.0%}" for n, v in top))


def _metrics_dict(m: Metrics | None) -> dict[str, Any] | None:
    return asdict(m) if m else None


def _write(
    out: Path, name: str, study: Study, report: Report, config: dict[str, Any], elapsed: float
) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    stem = out / f"{name}-{config['symbol']}-{config['interval']}-{stamp}"
    bars = study.bars

    def window(w: Any) -> list[str]:
        return [bars[w.start].ts.isoformat(), bars[w.end - 1].ts.isoformat()]

    document = {
        "config": config,
        "elapsed_seconds": round(elapsed, 1),
        "folds": [
            {
                "fold": f.fold.number,
                "train": window(f.fold.train),
                "test": window(f.fold.test),
                "values": f.values,
                "train_value": f.train_value,
                "test_value": f.test_value,
                "test_trades": f.test.trades,
                "trials": f.trials,
                "rejected": f.rejected,
                "stability_ratio": f.stability.ratio if f.stability else None,
                "stability_sharp": f.stability.sharp if f.stability else None,
                "deflated_sharpe": f.deflated,
                "note": f.note,
            }
            for f in report.folds
        ],
        "final_values": report.final_values,
        "holdout_window": window(report.plan.holdout),
        "results": {
            "hand_tuned_in_sample": _metrics_dict(report.baseline_research.metrics),
            **{
                v.name: {
                    "out_of_sample": _metrics_dict(v.chain.metrics()),
                    "holdout": _metrics_dict(_holdout_metrics(v)),
                }
                for v in [report.baseline, report.tuned, *report.others]
            },
        },
        "filters": report.extra.get("filters", []),
        "oos_probability_sharpe_positive": report.oos_probability(),
        "oos_mean_deflated_sharpe": report.oos_deflated(),
    }
    stem.with_suffix(".json").write_text(json.dumps(document, indent=2, default=str))

    trials = report.extra.get("trials", [])
    if trials:
        keys = list(dict.fromkeys(k for row in trials for k in row))
        with stem.with_name(stem.name + "-trials.csv").open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=keys)
            writer.writeheader()
            writer.writerows(trials)
    return stem


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("spec", type=Path, help="a strategy spec with a params block")
    parser.add_argument("--source", default="binance")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--interval", default="5m")
    parser.add_argument("--days", type=int, default=1095)
    parser.add_argument("--objective", choices=OBJECTIVES, default="sharpe")
    parser.add_argument("--trials", type=int, default=100, help="settings tried per fold")
    parser.add_argument(
        "--min-trades", type=int, default=30, help="a trial below this scores nothing"
    )
    parser.add_argument("--drawdown-penalty", type=float, default=1.0)
    parser.add_argument("--train-months", type=float, default=12)
    parser.add_argument("--test-months", type=float, default=3)
    parser.add_argument("--step-months", type=float, default=None)
    parser.add_argument("--holdout", type=float, default=0.15)
    parser.add_argument("--warmup-days", type=float, default=30)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--workers", type=int, default=None, help="processes; 1 runs in-process")
    parser.add_argument("--ml", action="store_true", help="also fit the ML entry filter")
    parser.add_argument("--capital", type=float, default=1000.0)
    parser.add_argument("--leverage", type=float, default=1.0)
    parser.add_argument("--slippage-bps", type=float, default=1.0)
    parser.add_argument("--funding-source", default="binance")
    parser.add_argument("--store", type=Path, default=paths.bars_path())
    parser.add_argument("--out", type=Path, default=paths.LOGS_DIR / "optimize")
    args = parser.parse_args()

    raw = json.loads(args.spec.read_text())
    if not declared(raw):
        parser.error(f"{args.spec} declares no params, so there is nothing to search")
    interval = Interval(args.interval)

    store, note = _open_store(args.store)
    if note:
        print(f"note: {note}")
    service = BarService(store)
    series = Series(args.source, args.symbol, interval, args.days)
    prepared = prepare(
        service,
        store,
        series,
        funding_source=args.funding_source,
        floors=Floors(),
    )
    bars = prepared.bars
    execution = Execution(
        capital=args.capital, leverage=args.leverage, slippage_bps=args.slippage_bps
    )
    study = Study(prepared, execution)
    warmup = int(args.warmup_days * 86400 / interval.seconds)
    plan = make_plan(
        bars,
        train_months=args.train_months,
        test_months=args.test_months,
        step_months=args.step_months,
        holdout=args.holdout,
        warmup=warmup,
    )
    goal = Goal(args.objective, args.min_trades, args.drawdown_penalty)

    fit: FitFilter | None = None
    if args.ml:
        from backtest.ml import MLConfig, make_fit_filter

        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        models = args.out / "models" / f"{args.spec.stem}-{args.symbol}-{interval}-{stamp}"
        fit = make_fit_filter(MLConfig(save_dir=models), seed=args.seed)

    print(
        f"{raw.get('name', args.spec.stem)}  {args.symbol} {interval}  "
        f"{len(bars):,} bars {_day(bars[0].ts)}..{_day(bars[-1].ts)}\n"
        f"{len(plan.folds)} folds of {args.train_months:g}m train + {args.test_months:g}m test, "
        f"{args.trials} trials each; holdout {_day(bars[plan.holdout.start].ts)}.. "
        f"({plan.holdout.bars:,} bars); objective {args.objective}, "
        f">= {args.min_trades} trades; defaults {_params(defaults(raw))}"
    )

    began = time.perf_counter()
    with Workers(study, args.workers) as workers:
        print(f"{workers.count} worker(s)")
        report = walk_forward(
            study,
            workers,
            raw,
            plan,
            goal,
            trials=args.trials,
            seed=args.seed,
            fit_filter=fit,
            progress=lambda message: print("  " + message, flush=True),
        )
    elapsed = time.perf_counter() - began

    _print_folds(study, report)
    _print_results(report)
    _print_filters(report)
    runs = sum(1 for row in report.extra.get("trials", []) if "trial" in row)
    print(f"\n{elapsed:.0f}s for {runs:,} scored trials ({runs / max(elapsed, 1e-9):.1f}/s)")

    config = {
        "spec": str(args.spec),
        "symbol": args.symbol,
        "source": args.source,
        "interval": str(interval),
        "days": args.days,
        "objective": args.objective,
        "trials": args.trials,
        "min_trades": args.min_trades,
        "train_months": args.train_months,
        "test_months": args.test_months,
        "holdout": args.holdout,
        "seed": args.seed,
        "ml": args.ml,
        "capital": args.capital,
        "leverage": args.leverage,
        "slippage_bps": args.slippage_bps,
    }
    stem = _write(args.out, args.spec.stem, study, report, config, elapsed)
    print(f"written: {stem}.json (+ -trials.csv)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

