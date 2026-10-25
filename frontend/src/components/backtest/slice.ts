import type { BacktestTrade } from "../../api";

/**
 * Slicing a finished run without running it again.
 *
 * Each trade is judged by when it opened. A slice is the trades already taken, so
 * the figures are exact for what they total; they are not what the strategy would
 * have done if it had only been allowed to trade in that slice, because holding
 * one position decides whether the next is taken.
 */
export interface Slice {
  side: "" | "long" | "short";
  /** A key of ZONES, or "". */
  zone: string;
  weekdays: string[];
  ended: string;
  year: string;
}

export const NO_SLICE: Slice = { side: "", zone: "", weekdays: [], ended: "", year: "" };

export const isSliced = (s: Slice) => JSON.stringify(s) !== JSON.stringify(NO_SLICE);

export const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

/** Local opening hours, in minutes after midnight, in the zone's own clock. */
interface Hours {
  tz: string;
  from: number;
  to: number;
}

/**
 * The trading sessions as the backend defines them (`backtest/sessions.py`), in
 * the order a trade is placed in them: a trade in the London/New York overlap is
 * London's. "quiet" is none of the three.
 */
const SESSIONS: [string, Hours][] = [
  ["london", { tz: "Europe/London", from: 8 * 60, to: 16 * 60 + 30 }],
  ["newyork", { tz: "America/New_York", from: 8 * 60, to: 17 * 60 }],
  ["tokyo", { tz: "Asia/Tokyo", from: 9 * 60, to: 18 * 60 }],
];

export const ZONES: [string, string][] = [
  ["london", "London"],
  ["newyork", "New York"],
  ["tokyo", "Tokyo"],
  ["quiet", "Quiet hours"],
];

const clocks = new Map<string, Intl.DateTimeFormat>();

function minutesIn(tz: string, at: Date): number {
  let fmt = clocks.get(tz);
  if (!fmt) {
    fmt = new Intl.DateTimeFormat("en-GB", {
      timeZone: tz,
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    });
    clocks.set(tz, fmt);
  }
  const parts = fmt.formatToParts(at);
  const get = (type: string) => Number(parts.find((p) => p.type === type)?.value ?? 0);
  return get("hour") * 60 + get("minute");
}

/** A timestamp from the backend, read as UTC when it carries no offset. */
export function instant(iso: string): Date {
  return new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? iso : `${iso}Z`);
}

export function zoneOf(iso: string): string {
  const at = instant(iso);
  for (const [key, h] of SESSIONS) {
    const m = minutesIn(h.tz, at);
    if (m >= h.from && m < h.to) return key;
  }
  return "quiet";
}

const weekdayOf = (iso: string) => WEEKDAYS[(instant(iso).getUTCDay() + 6) % 7];

export const yearsIn = (trades: BacktestTrade[]) =>
  [...new Set(trades.map((t) => instant(t.opened_at).getUTCFullYear().toString()))].sort();

export const endingsIn = (trades: BacktestTrade[]) => [
  ...new Set(trades.map((t) => t.exit_reason)),
];

export function apply(trades: BacktestTrade[], s: Slice): BacktestTrade[] {
  if (!isSliced(s)) return trades;
  return trades.filter((t) => {
    if (s.side && t.side !== s.side) return false;
    if (s.zone && zoneOf(t.opened_at) !== s.zone) return false;
    if (s.weekdays.length && !s.weekdays.includes(weekdayOf(t.opened_at))) return false;
    if (s.ended && t.exit_reason !== s.ended) return false;
    if (s.year && instant(t.opened_at).getUTCFullYear().toString() !== s.year) return false;
    return true;
  });
}

export interface Figures {
  trades: number;
  wins: number;
  gross: number;
  costs: number;
  net: number;
}

export function figures(trades: BacktestTrade[]): Figures {
  let wins = 0;
  let gross = 0;
  let net = 0;
  for (const t of trades) {
    if (t.net > 0) wins += 1;
    gross += t.gross;
    net += t.net;
  }
  return { trades: trades.length, wins, gross, costs: gross - net, net };
}
