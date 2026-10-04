import type { OptbtExpiryChoice, OptbtLegIn } from "../../api";

/** Which of a series: counted nearest first. */
export const NTH = ["1st", "2nd", "3rd"];

/** The nearest weekly: what a strategy trades unless told otherwise. */
export const NEAREST_WEEKLY: OptbtExpiryChoice = { series: "weekly", nth: 1, min_left: 0, days: 45 };

/** A leg as the form holds it: every field editable, percentages as the user types them. */
export interface LegDraft {
  id: number;
  side: "buy" | "sell";
  kind: "CE" | "PE";
  lots: number;
  /** 0 trades the strategy's expiry; 1-3 this leg's own nth of the same series. */
  expiryNth: number;
  /** "atm" with an offset, "premium" with a target premium, "pct" from spot,
      "straddle_width"/"sp_pct" sized off the ATM straddle's own premium. */
  strikeMode: "atm" | "premium" | "pct" | "delta" | "straddle_width" | "sp_pct";
  offset: number;
  premium: number;
  pct: number;
  delta: number;
  /** Multiple of the ATM straddle's premium, for "straddle_width". */
  widthMult: number;
  /** Percent of the ATM straddle's premium, for "sp_pct". */
  spPct: number;
  stopKind: "none" | "pct" | "points";
  stopValue: number;
  targetKind: "none" | "pct" | "points";
  targetValue: number;
}

let nextId = 1;

export function leg(
  side: "buy" | "sell",
  kind: "CE" | "PE",
  offset = 0,
  stop: { kind: "none" | "pct" | "points"; value: number } = { kind: "none", value: 25 },
): LegDraft {
  return {
    id: nextId++,
    side,
    kind,
    lots: 1,
    expiryNth: 0,
    strikeMode: "atm",
    offset,
    premium: 50,
    pct: 4,
    delta: 0.3,
    widthMult: 1,
    spPct: 25,
    stopKind: stop.kind,
    stopValue: stop.value,
    targetKind: "none",
    targetValue: 50,
  };
}

export function copyLeg(l: LegDraft): LegDraft {
  return { ...l, id: nextId++ };
}

const QUARTER = { kind: "pct" as const, value: 25 };

/** What a new strategy starts as: the short straddle, 25% stop on each leg. */
export function defaultLegs(): LegDraft[] {
  return [leg("sell", "CE", 0, QUARTER), leg("sell", "PE", 0, QUARTER)];
}

/** A leg as a spec holds it, back into the form - `toRequest` undone. */
export function fromRequest(l: OptbtLegIn): LegDraft {
  const level = (lv: OptbtLegIn["stop"], fallback: number) =>
    lv === null
      ? { kind: "none" as const, value: fallback }
      : {
          kind: lv.kind,
          value: lv.kind === "pct" ? Math.round(lv.value * 1000) / 10 : lv.value,
        };
  const stop = level(l.stop, 25);
  const target = level(l.target, 50);
  return {
    ...leg(l.side, l.kind),
    lots: l.lots,
    expiryNth: l.expiry ? l.expiry.nth : 0,
    strikeMode: l.strike.mode,
    offset: l.strike.offset,
    premium: l.strike.premium,
    pct: l.strike.pct,
    delta: l.strike.delta,
    widthMult: l.strike.width_mult,
    spPct: l.strike.sp_pct,
    stopKind: stop.kind,
    stopValue: stop.value,
    targetKind: target.kind,
    targetValue: target.value,
  };
}

export function toRequest(l: LegDraft, expiry: OptbtExpiryChoice): OptbtLegIn {
  const level = (kind: "none" | "pct" | "points", value: number) =>
    kind === "none" ? null : { kind, value: kind === "pct" ? value / 100 : value };
  return {
    side: l.side,
    kind: l.kind,
    lots: l.lots,
    expiry: l.expiryNth && expiry.series !== "days" ? { ...expiry, nth: l.expiryNth } : null,
    strike: {
      mode: l.strikeMode,
      offset: l.offset,
      premium: l.premium,
      pct: l.pct,
      delta: l.delta,
      width_mult: l.widthMult,
      sp_pct: l.spPct,
    },
    stop: level(l.stopKind, l.stopValue),
    target: level(l.targetKind, l.targetValue),
  };
}

