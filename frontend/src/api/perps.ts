import { getJson, postJson, request } from "./http";

export interface Venue {
  id: string;
  name: string;
  asset_class: "index_options" | "perpetuals" | "crypto_options";
  quote_currency: string;
  session: string;
  capabilities: string[];
}

export interface PerpInstrument {
  symbol: string;
  name: string;
  quote_asset: string;
  /** The venue's own ceiling for this contract — 150× on BTC, 75× gold, 50× oil. */
  max_leverage: number;
  /** Smallest order the venue accepts at the current price. Usually set by a
      notional floor, so it moves with the price. */
  min_quantity: number;
  min_notional: number;
  /** Decimal places the venue prices in, so a tile does not invent precision. */
  price_dp: number;
  quantity_dp: number;
  open: boolean;
}

export interface PerpPrice {
  symbol: string;
  /** Null until the stream has carried it. Never zero, which would be a market
      at nothing. */
  price: number | null;
  /** Seconds since it arrived, so a dead stream reads as stale rather than current. */
  age_seconds: number | null;
  /** The venue's own 24-hour change, as a percentage. A market with no close has
      no yesterday of ours to measure against. */
  change_pct: number | null;
}

export interface StreamStatus {
  connected: boolean;
  ticks: number;
  dropped: number;
  subscribers: number;
}

export interface PerpPosition {
  symbol: string;
  name: string;
  side: string;
  quantity: number;
  entry_price: number;
  price: number | null;
  leverage: number;
  margin_type: string;
  /** In the desk's money currency, not the currency the price is in. */
  margin: number;
  /** The same in the account's money — what it is actually debited. */
  margin_in_margin_asset: number | null;
  /** Margin currency per unit of quote currency, for showing a live figure in
      the money the account is kept in. */
  conversion_rate: number | null;
  unrealized_pnl: number | null;
  /** True when we worked the P&L out from the price because the venue gave none.
      A figure we derived should not be shown as the venue's. */
  pnl_is_ours: boolean;
  liquidation_price: number | null;
  /** Fraction of price. Comparable across instruments; a points difference is not. */
  liquidation_distance: number | null;
  position_id: string;
  /** Whether the exchange is holding a stop for this position. An exchange-held
      stop fires with this app closed; its absence means nothing closes the
      position but the market. */
  protected: boolean;
  take_profit_orders: number;
  stop_loss_orders: number;
}

export interface PerpOrder {
  symbol: string;
  side: "BUY" | "SELL";
  order_type: "MARKET" | "LIMIT";
  quantity: number;
  leverage: number;
  /** ISOLATED risks only the margin behind the position; CROSS puts the rest of
      the account behind it. */
  margin_mode: "ISOLATED" | "CROSS";
  limit_price?: number | null;
}

export interface OrderCheck {
  passed: boolean;
  reason: string;
}

export interface PerpOrderResult {
  /** Whether it actually left. False means refused, and `reasons` says why. */
  sent: boolean;
  checks: OrderCheck[];
  reasons: string[];
  /** What happened, in the venue's words when the venue decided. */
  outcome: string;
  notional: number;
  price: number | null;
  venue_order_id: string | null;
  record_id: number;
}

export function placePerpOrder(order: PerpOrder): Promise<PerpOrderResult> {
  return postJson<PerpOrder, PerpOrderResult>("/api/perps/orders", order);
}

export interface CloseResult {
  closed: boolean;
  outcome: string;
  venue_order_id: string | null;
  record_id: number;
}

/** Close a position at the market, for its full size. Reduce-only at the venue. */
export function closePerpPosition(positionId: string): Promise<CloseResult> {
  return postJson<Record<string, never>, CloseResult>(
    `/api/perps/positions/${encodeURIComponent(positionId)}/close`,
    {},
  );
}

export interface Protection {
  quantity: number;
  take_profit?: number | null;
  stop_loss?: number | null;
}

/** Ask the venue to hold a take-profit and stop-loss against a position. */
export function setProtection(positionId: string, body: Protection): Promise<void> {
  return request<void>(
    "POST",
    `/api/perps/positions/${encodeURIComponent(positionId)}/protection`,
    body,
  );
}

export interface PerpsDesk {
  venue: string;
  name: string;
  /** Prices and charts are in this. */
  quote_currency: string;
  /** Balances and P&L are in this, which is not the same on this venue. */
  money_currency: string;
  instruments: PerpInstrument[];
  prices: PerpPrice[];
  positions: PerpPosition[];
  /** Why the list is empty, when it is empty because something failed. "No
      positions" is a dangerous thing to show wrongly on a leveraged book. */
  positions_error: string | null;
  stream: StreamStatus;
}

export function getVenues(): Promise<Venue[]> {
  return getJson<Venue[]>("/api/venues");
}

export function getPerpsDesk(): Promise<PerpsDesk> {
  return getJson<PerpsDesk>("/api/perps");
}
