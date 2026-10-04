import { del, getJson, postJson } from "./http";

/** A leg as the page holds it and sends it. Null prices are filled by the server. */
export interface SimLegIn {
  id: string;
  side: "buy" | "sell";
  kind: "CE" | "PE";
  strike: number;
  expiry: string;
  lots: number;
  entry_at: string;
  entry_price: number | null;
  /** Resting levels, as option prices. */
  stop: number | null;
  target: number | null;
  exit_at: string | null;
  exit_price: number | null;
  exit_reason: string | null;
  enabled: boolean;
}

export interface SimLeg extends SimLegIn {
  status: "pending" | "open" | "closed" | "error";
  lot_size: number | null;
  ltp: number | null;
  ltp_at: string | null;
  iv: number | null;
  error: string | null;
  /** Charges and slippage, an open leg's exit at its last price included. */
  charges: number;
  slippage: number;
}

export interface SimSide {
  ltp: number;
  last_at: string;
  oi: number;
  volume: number;
  iv: number | null;
  delta: number | null;
  /** The book's top, on a live chain. Absent on a replayed one, which has only trades. */
  bid?: number | null;
  ask?: number | null;
}

export interface SimRow {
  strike: number;
  ce: SimSide | null;
  pe: SimSide | null;
}

export interface SimPayoff {
  pnl: number;
  realised: number;
  expiry_curve: [number, number][];
  today_curve: [number, number][];
  max_profit: number | null;
  max_loss: number | null;
  profit_unlimited: boolean;
  loss_unlimited: boolean;
  breakevens: number[];
  pop: number | null;
  /** Spot at -2, -1, +1, +2 standard deviations by the nearest expiry. */
  sd: number[];
  /** Margin today's rules would ask for the open legs: SPAN and exposure. */
  span: number;
  exposure: number;
  /** Charges and slippage of the included legs; `net` is `pnl` less them. */
  charges: number;
  net: number;
}

export interface SimMoment {
  at: string;
  first: string;
  last: string;
  spot: number;
  vix: number | null;
  future_expiry: string | null;
  future: number | null;
  expiries: SimExpiry[];
  expiry: string | null;
  lot_size: number | null;
  atm: number | null;
  atm_iv: number | null;
  rows: SimRow[];
  legs: SimLeg[];
  payoff: SimPayoff;
  /** What is being fetched for this moment, if anything. */
  loading: SimFetch | null;
  /** The whole position squared off by its P&L rule on the way here. */
  squared: { reason: "portfolio stop" | "portfolio target"; at: string; net: number } | null;
}

/** Square everything off at this net P&L, in rupees. */
export interface SimRule {
  stop: number | null;
  target: number | null;
}

/** Slippage as a fraction of premium with a rupee floor; brokerage per order. */
export interface SimCosts {
  slippage: number;
  min_slip: number;
  brokerage: number;
}

export interface SimExpiry {
  expiry: string;
  days: number;
  monthly: boolean;
}

/** What the store lacked, being fetched from Fyers: a day's session, or an expiry. */
export interface SimFetch {
  underlying: string;
  day: string;
  expiry: string | null;
  state: "index" | "listing" | "fetching" | "done" | "failed";
  total: number;
  done: number;
  bars: number;
  failed: number;
  error: string | null;
  started_at: string;
  finished_at: string | null;
}

/** No moment yet: the day it asked for is being fetched. */
export interface SimLoading {
  loading: SimFetch;
}

/** A month as the store knows it: sessions held, expiries listed, and the span held. */
export interface SimMonth {
  sessions: string[];
  expiries: string[];
  first: string | null;
  last: string | null;
}

export function getSimCalendar(underlying: string, month: string): Promise<SimMonth> {
  return getJson<SimMonth>(`/api/sim/calendar?underlying=${encodeURIComponent(underlying)}&month=${month}`);
}

export function getSimUnderlyings(): Promise<string[]> {
  return getJson<string[]>("/api/sim/underlyings");
}

export function getSimFetch(): Promise<SimFetch | null> {
  return getJson<SimFetch | null>("/api/sim/fetch");
}

export interface SimMomentRequest {
  underlying: string;
  at: string | null;
  /** "sod", "eod", "+5m", "-1h", "+1d" … */
  move: string | null;
  expiry: string | null;
  /** The moment shown before; a step forward from it fires stops and targets. */
  since: string | null;
  multiplier: number;
  legs: SimLegIn[];
  rule: SimRule;
  costs: SimCosts;
}

export interface SimSession {
  id: number;
  name: string;
  underlying: string;
  at: string;
  state: SimState;
  saved_at: string;
}

/** What a saved session restores. */
export interface SimState {
  legs: SimLegIn[];
  expiry: string | null;
  multiplier: number;
  /** The P&L rule as the page holds it, and the costs. Missing in older saves. */
  exitAll?: ExitAll;
  costs?: SimCosts;
}

/** The P&L rule as set: in rupees, or in percent of the margin when it was set. */
export interface ExitAll {
  unit: "rs" | "pct";
  stop: number | null;
  target: number | null;
  /** The margin a percentage was set against. */
  base: number | null;
}

export function getSimMoment(request: SimMomentRequest): Promise<SimMoment | SimLoading> {
  return postJson<SimMomentRequest, SimMoment | SimLoading>("/api/sim/moment", request);
}

export function getSimSessions(): Promise<SimSession[]> {
  return getJson<SimSession[]>("/api/sim/sessions");
}

export function saveSimSession(body: {
  id: number | null;
  name: string;
  underlying: string;
  at: string;
  state: SimState;
}): Promise<SimSession> {
  return postJson<typeof body, SimSession>("/api/sim/sessions", body);
}

export function deleteSimSession(id: number): Promise<void> {
  return del(`/api/sim/sessions/${id}`);
}
