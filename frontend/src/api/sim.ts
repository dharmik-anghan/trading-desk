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
}

export interface SimSide {
  ltp: number;
  last_at: string;
  oi: number;
  volume: number;
  iv: number | null;
  delta: number | null;
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
}

export interface SimMoment {
  at: string;
  first: string;
  last: string;
  spot: number;
  vix: number | null;
  future_expiry: string | null;
  future: number | null;
  expiries: { expiry: string; days: number; monthly: boolean }[];
  expiry: string | null;
  lot_size: number | null;
  atm: number | null;
  atm_iv: number | null;
  rows: SimRow[];
  legs: SimLeg[];
  payoff: SimPayoff;
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
}

export function getSimMoment(request: SimMomentRequest): Promise<SimMoment> {
  return postJson<SimMomentRequest, SimMoment>("/api/sim/moment", request);
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
