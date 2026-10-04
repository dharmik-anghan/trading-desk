import { getJson, postJson } from "./http";

/** What the option store holds. Runs outside it have nothing to trade. */
export interface OptbtCoverage {
  store: string;
  underlying: string;
  first_day: string | null;
  last_day: string | null;
  expiries_listed: number;
  expiries_held: number;
  first_expiry: string | null;
  last_expiry: string | null;
  contracts: number;
  bars: number;
}

/**
 * Which expiry: the `nth` of a series, passing over any with fewer than
 * `min_left` trading sessions left (1 skips an expiry on its own day).
 * "days" is the monthly nearest `days` calendar days out. "daily" is every
 * listed expiry; on the NSE it is the same as "weekly".
 */
export interface OptbtExpiryChoice {
  series: "daily" | "weekly" | "monthly" | "days";
  nth: number;
  min_left: number;
  days: number;
}

export interface OptbtLevel {
  kind: "pct" | "points";
  /** A fraction for pct: 0.25 is 25%. */
  value: number;
}

export interface OptbtLegIn {
  side: "buy" | "sell";
  kind: "CE" | "PE";
  lots: number;
  /** This leg's own expiry; null trades the strategy's. */
  expiry: OptbtExpiryChoice | null;
  /** atm: `offset` strikes from the money. premium: nearest to `premium`. pct: `pct`% from spot.
      straddle_width: `width_mult` x the ATM straddle's premium, away from ATM.
      sp_pct: the strike whose own premium is nearest `sp_pct`% of the ATM straddle's. */
  strike: {
    mode: "atm" | "premium" | "pct" | "delta" | "straddle_width" | "sp_pct";
    offset: number;
    premium: number;
    pct: number;
    /** Absolute delta to aim for, e.g. 0.30. */
    delta: number;
    width_mult: number;
    sp_pct: number;
  };
  stop: OptbtLevel | null;
  target: OptbtLevel | null;
}

/** Which days to trade. Null bounds mean no condition. */
export interface OptbtDays {
  expiry_day: "any" | "only" | "skip" | "skip_eve";
  dte_min: number | null;
  dte_max: number | null;
  vix_min: number | null;
  vix_max: number | null;
  vix_pct_min: number | null;
  vix_pct_max: number | null;
  vix_lookback: number;
  gap_min: number | null;
  gap_max: number | null;
  /** Pivot zones the open must sit in; empty means anywhere. */
  open_zones: string[];
}

/** Move the untested side in when spot reaches a condor's wing. Distances in index points. */
export interface OptbtAdjust {
  enabled: boolean;
  /** Spot within this many points of a long strike. */
  near_points: number;
  /** On a fall: the new short call is placed this many points above the long (or short) put. */
  fall_from: "long" | "short";
  fall_points: number;
  /** On a rise: the new short put is placed this many points below the short (or long) call. */
  rise_from: "long" | "short";
  rise_points: number;
  /** Move the wing with the short, keeping the spread's width. */
  move_wing: boolean;
  max_per_trade: number;
}

export const PIVOT_ZONES = ["below S2", "S2-S1", "S1-P", "P-R1", "R1-R2", "above R2"] as const;

/** When the entry fires. "time": the clock alone. "move_pct": spot must move
    this far from its price at `entry` first. "range_breakout": spot must close
    outside the high-low range formed between `entry` and `range_until`. */
export interface OptbtTrigger {
  mode: "time" | "move_pct" | "range_breakout";
  move_pct: number;
  range_until: string | null;
}

/** Trying the same legs again after the position goes flat, same day. */
export interface OptbtReEntry {
  enabled: boolean;
  /** What the previous attempt has to have ended on. */
  trigger: "leg_stop" | "mtm_stop" | "any";
  max_times: number;
}

/** One side of an indicator condition. Only the fields its kind uses matter. */
export interface OptbtOperand {
  kind: "price" | "ema" | "sma" | "rsi" | "supertrend" | "level" | "number";
  length: number;
  /** ATR multiple, for supertrend. */
  mult: number;
  /** Pivots from yesterday; PDH/PDL/PDC yesterday's high/low/close; DO today's open. */
  level: "P" | "R1" | "R2" | "R3" | "S1" | "S2" | "S3" | "PDH" | "PDL" | "PDC" | "DO";
  value: number;
}

/** Two operands compared on one timeframe's finished candles. */
export interface OptbtCondition {
  left: OptbtOperand;
  op: "above" | "below" | "crosses_above" | "crosses_below";
  right: OptbtOperand;
  /** Minutes per candle. */
  timeframe: 1 | 3 | 5 | 10 | 15 | 30 | 60;
}

/** take_if: take the entry only if the conditions hold. skip_if: skip it if they
    do. wait: enter on the first bar they hold, until the exit time. */
export interface OptbtEntrySignal {
  mode: "take_if" | "skip_if" | "wait";
  join: "all" | "any";
  conditions: OptbtCondition[];
}

/** Close the whole position when the conditions hold. */
export interface OptbtExitSignal {
  join: "all" | "any";
  conditions: OptbtCondition[];
}

export interface OptbtRunRequest {
  underlying: string;
  start: string;
  end: string;
  legs: OptbtLegIn[];
  expiry: OptbtExpiryChoice;
  entry: string;
  exit: string;
  /** Monday is 0. */
  weekdays: number[];
  hold: "intraday" | "expiry";
  mtm_stop: number | null;
  mtm_target: number | null;
  /** Fractions of the credit taken in. */
  target_credit: number | null;
  stop_credit: number | null;
  exit_dte: number | null;
  trail_to_cost: boolean;
  days: OptbtDays;
  adjust: OptbtAdjust;
  equal_wings: boolean;
  trigger: OptbtTrigger;
  reentry: OptbtReEntry;
  entry_signal: OptbtEntrySignal;
  exit_signal: OptbtExitSignal;
  slippage: number;
  min_slip: number;
  brokerage: number;
}

export interface OptbtLeg {
  tag: string;
  expiry: string;
  strike: number;
  kind: "CE" | "PE";
  side: "buy" | "sell";
  lots: number;
  lot_size: number;
  entry_at: string;
  entry: number;
  stop: number | null;
  exit_at: string | null;
  exit: number | null;
  ended: string | null;
  pnl: number;
  charges: number;
}

export interface OptbtTrade {
  id: number;
  opened: string;
  closed: string | null;
  ended: string;
  gross: number;
  charges: number;
  net: number;
  legs: OptbtLeg[];
  events: string[];
  /** The day at entry, for slicing results. */
  tags: OptbtTags;
  /** Lowest and highest gross P&L at any minute's close while open. */
  worst: number;
  best: number;
}

export interface OptbtTags {
  weekday: string;
  month: string;
  dte: number;
  /** Trading sessions to the first leg's expiry: 0 on the expiry session. */
  sessions_to_expiry: number;
  expiry_day: boolean;
  monthly_expiry: boolean;
  spot: number;
  vix: number | null;
  vix_pct: number | null;
  gap_pct: number | null;
  open_zone: string | null;
}

export interface OptbtSummary {
  trades: number;
  wins: number;
  win_rate: number;
  gross: number;
  charges: number;
  net: number;
  average: number;
  median: number;
  best: number;
  worst: number;
  profit_factor: number | null;
  max_drawdown: number;
  worst_share: number | null;
  cost_share: number | null;
  exits: Record<string, number>;
  by_year: Record<string, number>;
  abandoned_orders: number;
}

export interface OptbtResult {
  request: OptbtRunRequest;
  days: number;
  skipped: Record<string, number>;
  summary: OptbtSummary;
  charges: {
    brokerage: number;
    stt: number;
    exchange: number;
    sebi: number;
    stamp: number;
    gst: number;
    total: number;
  };
  /** [day, cumulative net] at each close. */
  equity: [string, number][];
  by_month: Record<string, number>;
  trades: OptbtTrade[];
}

export interface OptbtSeries {
  label: string;
  points: [string, number][];
}

export interface OptbtReplay {
  spot: OptbtSeries;
  legs: OptbtSeries[];
}

/** Every underlying the option store holds, with its tradable window. */
export function getOptbtUnderlyings(): Promise<OptbtCoverage[]> {
  return getJson<OptbtCoverage[]>("/api/optbt/underlyings");
}

export function runOptbt(request: OptbtRunRequest): Promise<OptbtResult> {
  return postJson<OptbtRunRequest, OptbtResult>("/api/optbt/run", request);
}

export function getOptbtReplay(request: {
  underlying?: string;
  start: string;
  end: string;
  legs: { expiry: string; strike: number; kind: "CE" | "PE" }[];
}): Promise<OptbtReplay> {
  return postJson<typeof request, OptbtReplay>("/api/optbt/replay", request);
}

// --- Relative rotation -----------------------------------------------------
