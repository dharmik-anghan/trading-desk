import { del, getJson, postJson } from "./http";
import type { OptbtRunRequest } from "./optbt";

/** A strategy without the window it is run over: what is saved and loaded. */
export type OptionStrategySpec = Omit<OptbtRunRequest, "underlying" | "start" | "end">;

export interface StrategyTemplate {
  id: string;
  name: string;
  say: string;
  spec: OptionStrategySpec;
}

export interface SavedStrategy {
  id: number;
  name: string;
  underlying: string;
  spec: OptionStrategySpec & { version?: number };
  created_at: string;
  saved_at: string;
}

export interface StrategySave {
  /** Overwrites that saved strategy; absent saves a new one. */
  id?: number;
  name: string;
  underlying: string;
  spec: OptionStrategySpec;
}

export const getStrategyTemplates = (): Promise<StrategyTemplate[]> => getJson("/api/strategies/templates");

export const getStrategies = (): Promise<SavedStrategy[]> => getJson("/api/strategies");

export const saveStrategy = (body: StrategySave): Promise<SavedStrategy> => postJson("/api/strategies", body);

export const deleteStrategy = (id: number): Promise<void> => del(`/api/strategies/${id}`);

/** An underlying Shark lists options on, with the terms it trades them on. */
export interface CryptoUnderlying {
  underlying: string;
  quote: string;
  spot: number | null;
  maker_fee_pct: number;
  taker_fee_pct: number;
  fee_cap_pct: number;
  min_im_pct: number;
  max_im_pct: number;
  mm_pct: number;
  /** One "lot" in a leg: the size an order moves in, in the coin. */
  qty_step: number;
  min_qty: number;
}

export const getCryptoUnderlyings = (): Promise<CryptoUnderlying[]> =>
  getJson("/api/crypto-options/underlyings");
