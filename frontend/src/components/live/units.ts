import { num } from "../../format";

/** How a market's quantities and money read: lots and rupees, or coins and USDT. */
export interface Unit {
  /** The column heading: "Lots", or "BTC". */
  label: string;
  /** Units in one of what `label` counts: a lot's size, or 1 for a coin. */
  per: number;
  /** Decimals on money. */
  dp: number;
  currency: string;
  /** What money is written with. */
  money: string;
  show: (qty: number) => string;
}

export function unitFor(currency: string, underlying: string, step: number): Unit {
  if (currency === "INR") {
    return {
      label: "Lots",
      per: step,
      dp: 0,
      currency,
      money: "₹",
      show: (qty) => num(qty / step, 0),
    };
  }
  const dp = step < 0.1 ? 2 : 1;
  return {
    label: underlying,
    per: 1,
    dp: 2,
    currency,
    money: currency,
    show: (qty) => num(qty, dp),
  };
}

export const istTime = (iso: string) =>
  new Date(iso).toLocaleTimeString("en-IN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
    timeZone: "Asia/Kolkata",
  });

export const shortDay = (iso: string) =>
  new Date(`${iso}T00:00:00`).toLocaleDateString("en-IN", { day: "2-digit", month: "short" });
