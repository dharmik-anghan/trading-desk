import { useEffect, useMemo, useState } from "react";
import { getBarSeries, runBacktest } from "../api";
import { BackButton } from "./BackButton";
import type {
  BacktestResult,
  BarSeries,
  Group,
  Level,
  Sizing,
  StrategySpec,
} from "../api";
import { ConditionList } from "./backtest/ConditionList";
import { LevelPicker } from "./backtest/LevelPicker";
import { SessionPicker } from "./backtest/SessionPicker";
import { TriggerPicker } from "./backtest/TriggerPicker";
import { RunResult } from "./backtest/RunResult";

interface Props {
  onHome: () => void;
}

const SOURCE: Record<string, string> = {
  shark: "Shark — the venue you trade",
  binance: "Binance — deep crypto history",
  yahoo: "Yahoo Finance — metals and energy",
  fyers: "Fyers — Indian options and indices",
};

/** Bar sizes a run can trade. Longer ones are built from what is stored. */
const SIZES = ["5m", "15m", "30m", "1h", "4h", "1d"] as const;

/** Where a higher-timeframe condition can read from. */
const HIGHER = ["15m", "30m", "1h", "4h", "1d"] as const;

/** A starting point that is a real strategy rather than an empty form. */
const OPENING: StrategySpec = {
  name: "EMA 9/21 crossover",
  long_entry: {
    all: [
      {
        left: { kind: "indicator", name: "ema", length: 9 },
        op: "crosses_above",
        right: { kind: "indicator", name: "ema", length: 21 },
      },
    ],
  },
  long_exit: {
    all: [
      {
        left: { kind: "indicator", name: "ema", length: 9 },
        op: "crosses_below",
        right: { kind: "indicator", name: "ema", length: 21 },
      },
    ],
  },
  short_entry: null,
  short_exit: null,
  stop: null,
  target: null,
};

const ema = (length: number, ago = 0, tf?: string) =>
  ({ kind: "indicator", name: "ema", length, ago, tf }) as const;
const rsi14 = { kind: "indicator", name: "rsi", length: 14 } as const;
const px = (field: "open" | "high" | "low" | "close", ago = 0) =>
  ({ kind: "price", field, ago }) as const;
const num = (value: number) => ({ kind: "value", value }) as const;

/**
 * Trend pullback: EMA 50 over 200 and rising, price back at the 50, a red candle
 * then a green one, RSI in 45-55, entered on a break of the green candle's high.
 * The short side is the mirror. Meant for 15m BTC and ETH.
 */
const PULLBACK: StrategySpec = {
  name: "Trend pullback · EMA 50/200 + RSI",
  trigger: { kind: "break", field: "high", ago: 0, within: 2 },
  long_entry: {
    all: [
      { left: ema(50), op: "above", right: ema(200) },
      { left: ema(50), op: "above", right: ema(50, 10) },
      { left: ema(200), op: "above", right: ema(200, 10) },
      { left: px("close"), op: "above", right: ema(50) },
      { left: px("low", 1), op: "below", right: ema(50, 1) },
      { left: px("close", 1), op: "below", right: px("open", 1) },
      { left: px("close"), op: "above", right: px("open") },
      { left: rsi14, op: "above", right: num(45) },
      { left: rsi14, op: "below", right: num(55) },
    ],
  },
  short_entry: {
    all: [
      { left: ema(50), op: "below", right: ema(200) },
      { left: ema(50), op: "below", right: ema(50, 10) },
      { left: ema(200), op: "below", right: ema(200, 10) },
      { left: px("close"), op: "below", right: ema(50) },
      { left: px("high", 1), op: "above", right: ema(50, 1) },
      { left: px("close", 1), op: "above", right: px("open", 1) },
      { left: px("close"), op: "below", right: px("open") },
      { left: rsi14, op: "above", right: num(45) },
      { left: rsi14, op: "below", right: num(55) },
    ],
  },
  long_exit: {
    any: [
      { left: rsi14, op: "above", right: num(70) },
      { left: rsi14, op: "below", right: num(45) },
    ],
  },
  short_exit: {
    any: [
      { left: rsi14, op: "below", right: num(30) },
      { left: rsi14, op: "above", right: num(55) },
    ],
  },
  stop: { kind: "swing", length: 5, value: 2 },
  target: null,
};

/**
 * Building a strategy and seeing what it would have done.
 *
 * The form is the strategy: there is no library of prewritten rules, because a
 * strategy is a choice of conditions and this is where they get chosen. What it
 * produces is sent to the backend as a tree and run over stored bars - the same
 * code that will later drive the live desk, stepping bar by bar and never seeing
 * a candle that had not closed.
 */
export function Backtesting({ onHome }: Props) {
  const [series, setSeries] = useState<BarSeries[] | null>(null);
  const [spec, setSpec] = useState<StrategySpec>(OPENING);
  const [source, setSource] = useState("binance");
  const [symbol, setSymbol] = useState("BTCUSDT");
  const [interval, setInterval] = useState<string>("4h");
  const [days, setDays] = useState(1095);
  const [capital, setCapital] = useState(1000);
  const [leverage, setLeverage] = useState(1);
  const [slippage, setSlippage] = useState(1);
  const [maker, setMaker] = useState(false);
  // How each position is sized. "quantity" is the lot you would type into the
  // ticket; the venue's own floors sit beside it, because a size it would refuse
  // is not a size a result may assume.
  const [sizing, setSizing] = useState<Sizing>("equity");
  const [risk, setRisk] = useState(100);
  const [lot, setLot] = useState(0.002);
  const [notional, setNotional] = useState(1000);
  const [minQuantity, setMinQuantity] = useState(0.002);
  const [minNotional, setMinNotional] = useState(115);
  const [quantityDp, setQuantityDp] = useState(3);
  const [result, setResult] = useState<BacktestResult | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void getBarSeries()
      .then(setSeries)
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
  }, []);

  // What can be tested at all. A symbol with nothing stored is a run that fails
  // with a 404, so it is better not offered.
  const instruments = useMemo(() => {
    const seen = new Map<string, string>();
    for (const s of series ?? []) seen.set(`${s.source}/${s.symbol}`, s.source);
    return [...seen.keys()].map((key) => {
      const [src, sym] = key.split("/");
      return { source: src, symbol: sym };
    });
  }, [series]);

  // Only the timeframes at or above what is stored: a 5m run needs 5m bars, and
  // no amount of arithmetic builds them out of hours.
  const shortest = useMemo(() => {
    const held = (series ?? [])
      .filter((s) => s.source === source && s.symbol === symbol)
      .map((s) => s.interval);
    return held.length ? held : ["5m"];
  }, [series, source, symbol]);

  const canRun = useMemo(
    () => Boolean(spec.long_entry || spec.short_entry) && !running,
    [spec, running],
  );

  const go = () => {
    setRunning(true);
    setError(null);
    void runBacktest({
      spec,
      source,
      symbol,
      interval,
      days,
      capital,
      leverage,
      slippage_bps: slippage,
      maker_entry: maker,
      sizing,
      risk: risk / 100,
      quantity: lot,
      notional,
      min_quantity: minQuantity,
      min_notional: minNotional,
      quantity_dp: quantityDp,
    })
      .then((r) => {
        setResult(r);
        setError(null);
      })
      .catch((e: unknown) => {
        setError(e instanceof Error ? e.message : String(e));
        setResult(null);
      })
      .finally(() => setRunning(false));
  };

  const total = (series ?? []).reduce((n, s) => n + s.bars, 0);

  return (
    <main className="bt">
      <header>
        <BackButton onClick={onHome} />
        <h1>Backtesting</h1>
        <p>
          {series === null
            ? "Reading what the store holds…"
            : total === 0
              ? "No bars stored yet. Run scripts/backfill_bars.py to fetch some history."
              : `${total.toLocaleString()} bars across ${series.length} series.`}
        </p>
      </header>

      {error && <p className="bad">{error}</p>}

      <div className="build">
        <section className="what">
          <h3>What to test it on</h3>
          <div className="row">
            <label>Instrument</label>
            <select
              value={`${source}/${symbol}`}
              onChange={(e) => {
                const [src, sym] = e.target.value.split("/");
                setSource(src);
                setSymbol(sym);
              }}
            >
              {instruments.map((i) => (
                <option key={`${i.source}/${i.symbol}`} value={`${i.source}/${i.symbol}`}>
                  {i.symbol} · {SOURCE[i.source] ?? i.source}
                </option>
              ))}
            </select>
          </div>
          <div className="row">
            <label>Bar size</label>
            <select value={interval} onChange={(e) => setInterval(e.target.value)}>
              {SIZES.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
            <small>built from the {shortest.join(", ")} held</small>
          </div>
          <div className="row">
            <label>Over</label>
            <input
              className="num wide"
              type="number"
              min={1}
              value={days}
              onChange={(e) => setDays(Math.max(1, Number(e.target.value)))}
            />
            <small>days</small>
          </div>
        </section>

        <section className="what">
          <h3>How it would be traded</h3>
          <div className="row">
            <label>Capital</label>
            <input
              className="num wide"
              type="number"
              min={1}
              value={capital}
              onChange={(e) => setCapital(Math.max(1, Number(e.target.value)))}
            />
            <small>USDT</small>
          </div>
          <div className="row">
            <label>Size each</label>
            <select value={sizing} onChange={(e) => setSizing(e.target.value as Sizing)}>
              <option value="equity">as a share of the account</option>
              <option value="quantity">as a fixed lot</option>
              <option value="notional">as a fixed amount of money</option>
            </select>
          </div>
          {sizing === "equity" && (
            <div className="row">
              <label />
              <input
                className="num"
                type="number"
                min={1}
                max={100}
                value={risk}
                onChange={(e) =>
                  setRisk(Math.min(100, Math.max(1, Number(e.target.value))))
                }
              />
              <small>% of the account — it compounds, so a losing run trades smaller</small>
            </div>
          )}
          {sizing === "quantity" && (
            <div className="row">
              <label />
              <input
                className="num wide"
                type="number"
                min={0}
                step="0.001"
                value={lot}
                onChange={(e) => setLot(Math.max(0, Number(e.target.value)))}
              />
              <small>contracts, every trade — the lot you would type in</small>
            </div>
          )}
          {sizing === "notional" && (
            <div className="row">
              <label />
              <input
                className="num wide"
                type="number"
                min={0}
                value={notional}
                onChange={(e) => setNotional(Math.max(0, Number(e.target.value)))}
              />
              <small>USDT at work, whatever the account is worth</small>
            </div>
          )}
          <div className="row">
            <label>Leverage</label>
            <input
              className="num"
              type="number"
              min={1}
              max={150}
              value={leverage}
              onChange={(e) => setLeverage(Math.max(1, Number(e.target.value)))}
            />
            <small>× — liquidation is modelled, so this bites</small>
          </div>
          <div className="row">
            <label>Slippage</label>
            <input
              className="num"
              type="number"
              min={0}
              step="0.5"
              value={slippage}
              onChange={(e) => setSlippage(Math.max(0, Number(e.target.value)))}
            />
            <small>basis points, always against you</small>
          </div>
          <div className="row">
            <label>Smallest</label>
            <input
              className="num wide"
              type="number"
              min={0}
              step="0.001"
              value={minQuantity}
              onChange={(e) => setMinQuantity(Math.max(0, Number(e.target.value)))}
              title="The venue's minimum quantity"
            />
            <input
              className="num"
              type="number"
              min={0}
              value={minNotional}
              onChange={(e) => setMinNotional(Math.max(0, Number(e.target.value)))}
              title="The venue's minimum order value, which usually binds first"
            />
            <small>lot / USDT the venue accepts</small>
          </div>
          <div className="row">
            <label>Decimals</label>
            <input
              className="num"
              type="number"
              min={0}
              max={8}
              value={quantityDp}
              onChange={(e) =>
                setQuantityDp(Math.min(8, Math.max(0, Number(e.target.value))))
              }
            />
            <small>a quantity is rounded down to this, never up</small>
          </div>
          <div className="row">
            <label>Entries</label>
            <select value={maker ? "maker" : "taker"} onChange={(e) => setMaker(e.target.value === "maker")}>
              <option value="taker">market — 0.047% a side</option>
              <option value="maker">resting limit — 0.019%, only if traded through</option>
            </select>
          </div>
        </section>
      </div>

      <div className="build">
        <section className="what wide">
          <h3>The strategy</h3>
          <div className="row">
            <label>Start from</label>
            <button
              type="button"
              onClick={() => {
                setSpec(PULLBACK);
                setInterval("15m");
                setDays(1095);
              }}
            >
              Trend pullback · 15m
            </button>
          </div>
          <div className="row">
            <label>Called</label>
            <input
              className="text"
              value={spec.name}
              onChange={(e) => setSpec({ ...spec, name: e.target.value })}
            />
          </div>

          <ConditionList
            label="Go long when"
            hint="Nothing — this strategy takes no longs."
            value={spec.long_entry ?? null}
            onChange={(g: Group | null) => setSpec({ ...spec, long_entry: g })}
            intervals={HIGHER}
            traded={interval}
          />
          <ConditionList
            label="Close longs when"
            hint="Nothing — longs are closed only by a stop, a target, or the end of the data."
            value={spec.long_exit ?? null}
            onChange={(g: Group | null) => setSpec({ ...spec, long_exit: g })}
            intervals={HIGHER}
            traded={interval}
          />
          <ConditionList
            label="Go short when"
            hint="Nothing — this strategy takes no shorts."
            value={spec.short_entry ?? null}
            onChange={(g: Group | null) => setSpec({ ...spec, short_entry: g })}
            intervals={HIGHER}
            traded={interval}
          />
          <ConditionList
            label="Close shorts when"
            hint="Nothing — shorts are closed only by a stop, a target, or the end of the data."
            value={spec.short_exit ?? null}
            onChange={(g: Group | null) => setSpec({ ...spec, short_exit: g })}
            intervals={HIGHER}
            traded={interval}
          />

          <TriggerPicker
            value={spec.trigger ?? null}
            onChange={(t) => setSpec({ ...spec, trigger: t })}
          />

          <SessionPicker
            sessions={(spec.sessions ?? []) as string[]}
            closeOutside={spec.close_outside_session ?? false}
            onSessions={(next) => setSpec({ ...spec, sessions: next })}
            onCloseOutside={(v) => setSpec({ ...spec, close_outside_session: v })}
          />

          <div className="levels">
            <LevelPicker
              label="Stop at"
              value={spec.stop ?? null}
              onChange={(l: Level | null) => setSpec({ ...spec, stop: l })}
            />
            <LevelPicker
              label="Target at"
              value={spec.target ?? null}
              onChange={(l: Level | null) => setSpec({ ...spec, target: l })}
              allowReward
            />
          </div>

          <div className="go">
            <button className="run" onClick={go} disabled={!canRun}>
              {running ? "Running…" : "Run it"}
            </button>
            {!spec.long_entry && !spec.short_entry && (
              <small>Add a long or a short entry first.</small>
            )}
          </div>
        </section>
      </div>

      {result && <RunResult result={result} spec={spec} />}

      {series !== null && series.length > 0 && (
        <details className="held-wrap">
          <summary>What history is held ({series.length} series)</summary>
          <table className="held">
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Size</th>
                <th>Source</th>
                <th>Bars</th>
                <th>From</th>
                <th>To</th>
              </tr>
            </thead>
            <tbody>
              {series.map((s) => (
                <tr key={`${s.source}/${s.symbol}/${s.interval}`}>
                  <td>
                    <b>{s.symbol}</b>
                  </td>
                  <td>{s.interval}</td>
                  <td>{SOURCE[s.source] ?? s.source}</td>
                  <td className="n">{s.bars.toLocaleString()}</td>
                  <td>{day(s.first)}</td>
                  <td>{day(s.last)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      )}

      <p className="note">
        A run is only as honest as the series under it. Stored bars are whatever a source
        served at the time and are not restated, so two runs over the same window read the
        same data. Signals fill at the next bar's open, a bar that reaches both the stop and
        the target is counted as the stop, and every cost is charged as it is incurred.
      </p>
    </main>
  );
}

function day(iso: string | null): string {
  if (!iso) return "—";
  const at = new Date(iso);
  return Number.isNaN(at.getTime()) ? "—" : at.toISOString().slice(0, 10);
}
