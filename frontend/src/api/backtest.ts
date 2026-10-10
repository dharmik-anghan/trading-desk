import type { Candle, IndicatorLine } from "./chart";
import { postJson } from "./http";

/** What an indicator operand can name. */
export type IndicatorName = "ema" | "sma" | "rsi" | "atr" | "pivot_gap" | "pivot_gap_rank";

/** One side of a comparison: an indicator, a price, a pivot level, or a number. */
export type Operand =
  | {
      kind: "indicator";
      name: IndicatorName;
      length: number;
      ago?: number;
      tf?: string;
    }
  | { kind: "price"; field: "open" | "high" | "low" | "close"; ago?: number; tf?: string }
  | { kind: "pivot"; level: string; ago?: number; tf?: string }
  | { kind: "value"; value: number };

export type Comparison = "crosses_above" | "crosses_below" | "above" | "below" | "equals";

export interface Compare {
  left: Operand;
  op: Comparison;
  right: Operand;
}

/** A group of comparisons, joined one way or the other. */
export interface Group {
  all?: Compare[];
  any?: Compare[];
}

export type LevelKind = "percent" | "candle" | "atr" | "pivot" | "swing" | "reward";

export interface Level {
  kind: LevelKind;
  value?: number;
  length?: number;
  field?: string;
  level?: string;
  ago?: number;
  tf?: string;
}

/** A named session, or a window of your own with a real timezone. */
export type SessionChoice = string | { name?: string; start: string; end: string; tz: string };

/** What has to happen after the conditions before a trade is actually taken. */
export interface Trigger {
  kind: "break";
  /** Which price of the setup candle the order rests at. Mirrors for a short. */
  field: "high" | "low" | "close" | "open";
  /** Which candle, counting back from the one the conditions fired on. */
  ago?: number;
  /** How many candles the order rests for. */
  within: number;
  /** A cushion past the level, in basis points, always against you. */
  buffer_bps?: number;
}

export interface StrategySpec {
  name: string;
  /** Absent means enter at the next candle's open. */
  trigger?: Trigger | null;
  /** Hours during which entries may fire. Empty means all of them. */
  sessions?: SessionChoice[];
  /** Whether an open position is closed when the session ends. */
  close_outside_session?: boolean;
  long_entry?: Group | null;
  short_entry?: Group | null;
  long_exit?: Group | null;
  short_exit?: Group | null;
  stop?: Level | null;
  target?: Level | null;
}

export type Sizing = "equity" | "quantity" | "notional";

export interface BacktestRequest {
  spec: StrategySpec;
  source: string;
  symbol: string;
  interval: string;
  days: number;
  capital: number;
  leverage: number;
  slippage_bps: number;
  maker_entry: boolean;
  /** How each position is sized. */
  sizing: Sizing;
  /** For "equity": the fraction of the account committed as margin. */
  risk: number;
  /** For "quantity": the lot, in the instrument's own units. */
  quantity: number;
  /** For "notional": the position's value in the quote currency. */
  notional: number;
  min_quantity: number;
  min_notional: number;
  quantity_dp: number;
}

export interface BacktestMetrics {
  trades: number;
  wins: number;
  win_rate: number;
  total_return: number;
  buy_and_hold: number;
  beat_holding: boolean;
  max_drawdown: number;
  /** Compounded yearly return, or null over a window too short to annualise. */
  annualised: number | null;
  sharpe: number;
  /** Yearly return over the worst drawdown. Null when nothing ever fell. */
  calmar: number | null;
  gross: number;
  fees: number;
  funding: number;
  slippage: number;
  cost_share: number | null;
  exposure: number;
  best: number;
  worst: number;
  endings: Record<string, number>;
  by_side: Record<string, SideSummary>;
  average_bars_held: number;
}

/** One side of the book on its own — a strategy written both ways is two strategies. */
export interface SideSummary {
  trades: number;
  wins: number;
  win_rate: number;
  gross: number;
  net: number;
}

export interface BacktestTrade {
  side: string;
  opened_at: string;
  closed_at: string;
  entry: number;
  exit_price: number;
  quantity: number;
  /** Position value at entry, in the quote currency. */
  notional: number;
  why: string;
  /** The condition that opened it, in the words it was built with. */
  entry_reason: string;
  /** What closed it — the exit condition, or the stop, target or liquidation. */
  exit_reason: string;
  gross: number;
  fees: number;
  funding: number;
  slippage: number;
  net: number;
  /** Net against the margin the position tied up, not against the whole account. */
  net_pct: number;
  bars_held: number;
}

export interface BacktestResult {
  name: string;
  /** The strategy read back in words, so a result says what produced it. */
  reads: string;
  symbol: string;
  source: string;
  interval: string;
  bars: number;
  started: string | null;
  ended: string | null;
  capital: number;
  final: number;
  metrics: BacktestMetrics;
  /** [unix seconds, equity], thinned for drawing. */
  curve: [number, number][];
  trades: BacktestTrade[];
  trades_total: number;
  /** Positions closed and reopened the other way on the same signal. */
  reversals: number;
  /** Setups that armed a resting order, and those price never reached. */
  armed: number;
  expired_unfilled: number;
  skipped_too_small: number;
  skipped_unaffordable: number;
  caveats: string[];
}

export interface WindowResponse {
  source: string;
  symbol: string;
  interval: string;
  candles: Candle[];
  lines: IndicatorLine[];
}

/**
 * Bars over one window, for looking at a single trade.
 *
 * Read from the store only. A backtest runs over history already fetched, and
 * going to a source here could return bars that differ from the ones the numbers
 * were computed on.
 */
export function getBacktestCandles(request: {
  source: string;
  symbol: string;
  interval: string;
  start: string;
  end: string;
  spec?: StrategySpec;
}): Promise<WindowResponse> {
  return postJson<typeof request, WindowResponse>("/api/backtest/candles", request);
}

export function runBacktest(request: BacktestRequest): Promise<BacktestResult> {
  return postJson<BacktestRequest, BacktestResult>("/api/backtest/run", request);
}

// --- Options backtesting ---------------------------------------------------
