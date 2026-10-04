import { del, getJson, postJson, request } from "./http";
import type { OptbtLevel } from "./optbt";
import type { SimPayoff } from "./sim";
import type { OptionStrategySpec } from "./strategies";

/** Something that can be traded live: an NSE index, or a coin. */
export interface LiveMarket {
  underlying: string;
  venue: string;
  currency: string;
  group: "NSE" | "Crypto";
  /** Whether real orders can be sent on it now. */
  live: boolean;
}

/** One option of a strike, live: the sim's side plus the book's top and mark. */
export interface LiveSide {
  ltp: number;
  last_at: string;
  oi: number;
  volume: number;
  iv: number | null;
  delta: number | null;
  bid: number | null;
  ask: number | null;
  mark: number | null;
  symbol: string;
}

export interface LiveRow {
  strike: number;
  ce: LiveSide | null;
  pe: LiveSide | null;
}

export interface LiveExpiry {
  expiry: string;
  days: number;
  monthly: boolean;
  token: string;
}

/** A paper leg, as the server keeps and values it. `qty` is in units. */
export interface LiveLeg {
  id: number;
  symbol: string;
  side: "buy" | "sell";
  kind: "CE" | "PE";
  strike: number;
  expiry: string;
  qty: number;
  entry_at: string;
  entry_price: number;
  stop: number | null;
  target: number | null;
  exit_at: string | null;
  exit_price: number | null;
  exit_reason: string | null;
  enabled: boolean;
  status: "open" | "closed";
  mark: number | null;
  bid: number | null;
  ask: number | null;
  iv: number | null;
  gross: number | null;
  /** Paid, and for an open leg what closing at the mark would add. */
  fees: number;
  net: number | null;
}

export interface LiveSession {
  id: number;
  name: string;
  /** "live" sessions trade real orders on the venue. */
  mode: "paper" | "live";
  venue: string;
  underlying: string;
  created_at: string;
  /** Square everything off at this net P&L, in the market's money. */
  rule_stop: number | null;
  rule_target: number | null;
  squared: { reason: "portfolio stop" | "portfolio target"; at: string; net: number } | null;
}

export interface LiveState {
  at: string;
  underlying: string;
  venue: string;
  currency: string;
  /** Whether orders can be filled now. */
  open: boolean;
  spot: number;
  /** The futures price the shown expiry is priced off. */
  forward: number;
  expiries: LiveExpiry[];
  expiry: string;
  expiry_token: string;
  atm: number | null;
  atm_iv: number | null;
  /** Orders move in steps of this many units: one lot, or 0.01 BTC. */
  step: number;
  min_qty: number;
  rows: LiveRow[];
  session: LiveSession | null;
  legs: LiveLeg[];
  payoff: SimPayoff | null;
  /** What the open legs would tie up. An estimate, by each market's rules. */
  margin: number;
  /** For a live session: what the venue itself says is held. */
  account: LiveAccount | null;
}

export interface LiveAccount {
  /** Free in the options wallet, in the quote currency. */
  available: number | null;
  positions: {
    symbol: string;
    side: "buy" | "sell";
    size: number;
    entry: number;
    mark: number | null;
    unrealised: number | null;
  }[];
  /** Where the venue's positions and the session's open legs disagree. */
  mismatches: string[];
  error: string | null;
}

/** A leg not yet traded. `qty` in units, levels as the strategy has them. */
export interface DraftLeg {
  key: string;
  side: "buy" | "sell";
  kind: "CE" | "PE";
  qty: number;
  symbol: string | null;
  strike: number | null;
  expiry: string | null;
  expiry_token: string | null;
  bid: number | null;
  ask: number | null;
  mark: number | null;
  stop: OptbtLevel | null;
  target: OptbtLevel | null;
  error: string | null;
}

/** A whole-position exit a strategy carries. */
export interface LiveRule {
  mtm_stop: number | null;
  mtm_target: number | null;
  stop_credit: number | null;
  target_credit: number | null;
}

export interface LivePreview {
  payoff: SimPayoff | null;
  margin: number;
  /** Taken in (+) or paid out (-), before fees. */
  premium: number;
  fees: number;
  problems: Record<string, string>;
}

export const getLiveMarkets = (): Promise<LiveMarket[]> => getJson("/api/live/markets");

export function getLiveState(params: {
  underlying: string;
  expiry: string;
  sessionId: number | null;
}): Promise<LiveState> {
  const q = new URLSearchParams({ underlying: params.underlying, expiry: params.expiry, strikes: "15" });
  if (params.sessionId !== null) q.set("session_id", String(params.sessionId));
  return getJson(`/api/live/state?${q}`);
}

export const resolveLive = (
  underlying: string,
  spec: OptionStrategySpec,
): Promise<{ legs: Omit<DraftLeg, "key">[]; rule: LiveRule }> =>
  postJson("/api/live/resolve", { underlying, spec });

type DraftIn = {
  symbol: string;
  side: "buy" | "sell";
  qty: number;
  stop?: OptbtLevel | null;
  target?: OptbtLevel | null;
};

export const previewLive = (underlying: string, legs: DraftIn[]): Promise<LivePreview> =>
  postJson("/api/live/preview", { underlying, legs });

export const placeLive = (body: {
  session_id: number | null;
  underlying: string;
  legs: DraftIn[];
  rule: LiveRule | null;
  mode: "paper" | "live";
  /** Must be true for a live basket. */
  confirm: boolean;
}): Promise<{ session: LiveSession; legs: LiveLeg[]; problem: string | null }> =>
  postJson("/api/live/orders", body);

export const getLiveSessions = (): Promise<LiveSession[]> => getJson("/api/live/sessions");

export const patchLiveSession = (
  id: number,
  body: { name?: string; rule_stop?: number | null; rule_target?: number | null },
): Promise<LiveSession> => request("PATCH", `/api/live/sessions/${id}`, body);

export const deleteLiveSession = (id: number): Promise<void> => del(`/api/live/sessions/${id}`);

/** `confirm` is required to close a live session's leg, which sends a real order. */
export const exitLiveLeg = (id: number, confirm = false): Promise<LiveLeg> =>
  postJson(`/api/live/legs/${id}/exit?confirm=${confirm}`, {});

export const patchLiveLeg = (
  id: number,
  body: { stop?: number | null; target?: number | null; enabled?: boolean },
): Promise<LiveLeg> => request("PATCH", `/api/live/legs/${id}`, body);

export const removeLiveLeg = (id: number): Promise<void> => del(`/api/live/legs/${id}`);

export const exitAllLive = (sessionId: number, confirm = false): Promise<{ problems: string[] }> =>
  postJson(`/api/live/sessions/${sessionId}/exit-all?confirm=${confirm}`, {});
