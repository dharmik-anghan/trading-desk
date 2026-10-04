import type { SimLeg, SimLegIn, SimRow } from "../../api";

/** A leg as the page holds it: what it sends, and what the server last said of it. */
export type Leg = SimLegIn & Partial<Omit<SimLeg, keyof SimLegIn>>;

const IN_KEYS: (keyof SimLegIn)[] = [
  "id",
  "side",
  "kind",
  "strike",
  "expiry",
  "lots",
  "entry_at",
  "entry_price",
  "stop",
  "target",
  "exit_at",
  "exit_price",
  "exit_reason",
  "enabled",
];

/** Only the fields the server accepts: it refuses any it does not know. */
export function toIn(leg: Leg): SimLegIn {
  return Object.fromEntries(IN_KEYS.map((k) => [k, leg[k]])) as unknown as SimLegIn;
}

let seq = 0;
export const newId = () => `${Date.now().toString(36)}${(seq++).toString(36)}`;

/** The same contract, entered at the same moment: the server's answer applies. */
const sameTrade = (a: SimLegIn, b: SimLegIn) =>
  a.side === b.side &&
  a.kind === b.kind &&
  a.strike === b.strike &&
  a.expiry === b.expiry &&
  a.entry_at === b.entry_at;

/**
 * The server's legs folded into the page's current ones.
 *
 * The page may have changed a leg while the request was out - autoplay keeps
 * them going. What the page set (lots, levels, the checkbox) stays; what only
 * the server knows (fills, exits it applied, prices) comes from it - but only
 * for a leg still the same trade, since a leg moved to another strike is
 * waiting on an answer of its own.
 */
export function merge(local: Leg[], server: SimLeg[]): Leg[] {
  const byId = new Map(server.map((s) => [s.id, s]));
  return local.map((leg) => {
    const s = byId.get(leg.id);
    if (!s || !sameTrade(leg, s)) return leg;
    const exitFromServer = leg.exit_at === null || leg.exit_at === s.exit_at;
    return {
      ...leg,
      entry_price: leg.entry_price ?? s.entry_price,
      exit_at: exitFromServer ? s.exit_at : leg.exit_at,
      exit_price: exitFromServer ? s.exit_price : leg.exit_price,
      exit_reason: exitFromServer ? s.exit_reason : leg.exit_reason,
      status: s.status,
      lot_size: s.lot_size,
      ltp: s.ltp,
      ltp_at: s.ltp_at,
      iv: s.iv,
      error: s.error,
    };
  });
}

/** Rupees the leg has made, at its exit or its last price. Null before a price. */
export function legPnl(leg: Leg, multiplier: number): number | null {
  if (leg.entry_price === null || !leg.lot_size || leg.status === "pending" || leg.status === "error")
    return null;
  const mark = leg.status === "closed" ? leg.exit_price : leg.ltp;
  if (mark === null || mark === undefined) return null;
  const sign = leg.side === "buy" ? 1 : -1;
  return sign * (mark - leg.entry_price) * leg.lots * leg.lot_size * multiplier;
}

/** A level as a percent of the entry, against the position: up for a short's stop. */
export function levelPct(leg: Leg, level: number | null, against: boolean): number | null {
  if (level === null || leg.entry_price === null || leg.entry_price <= 0) return null;
  const move = (level / leg.entry_price - 1) * 100;
  const sign = (leg.side === "sell") === against ? 1 : -1;
  return Math.round(sign * move * 10) / 10;
}

export function levelFrom(leg: Leg, pct: number | null, against: boolean): number | null {
  if (pct === null || leg.entry_price === null) return null;
  const sign = (leg.side === "sell") === against ? 1 : -1;
  return Math.max(0.05, Math.round(leg.entry_price * (1 + (sign * pct) / 100) * 20) / 20);
}

/** The spacing of listed strikes near the money, read from the chain. */
export function strikeStep(rows: SimRow[], atm: number | null): number {
  const near = rows
    .map((r) => r.strike)
    .filter((k) => atm === null || Math.abs(k - atm) <= atm * 0.03)
    .sort((a, b) => a - b);
  const gaps = new Map<number, number>();
  for (let i = 1; i < near.length; i++) {
    const g = near[i] - near[i - 1];
    gaps.set(g, (gaps.get(g) ?? 0) + 1);
  }
  let best = 50;
  let most = 0;
  for (const [g, n] of gaps) if (n > most) [best, most] = [g, n];
  return best;
}
