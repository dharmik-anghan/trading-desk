import { describe, expect, it } from "vitest";
import type { BacktestTrade } from "../../api";
import { NO_SLICE, apply, figures, instant, zoneOf } from "./slice";

const trade = (opened_at: string, over: Partial<BacktestTrade> = {}): BacktestTrade =>
  ({
    side: "long",
    opened_at,
    closed_at: opened_at,
    exit_reason: "the stop was reached",
    gross: 10,
    net: 6,
    ...over,
  }) as BacktestTrade;

describe("session of a trade", () => {
  it("puts the London morning in London", () => {
    expect(zoneOf("2026-06-10T09:00:00+00:00")).toBe("london");
  });

  it("follows daylight saving: 07:30 UTC is London in summer, not in winter", () => {
    expect(zoneOf("2026-07-01T07:30:00+00:00")).toBe("london");
    expect(zoneOf("2026-01-15T07:30:00+00:00")).not.toBe("london");
    expect(zoneOf("2026-01-15T23:00:00+00:00")).toBe("quiet");
  });

  it("gives the London/New York overlap to London", () => {
    expect(zoneOf("2026-06-10T13:30:00+00:00")).toBe("london");
  });

  it("calls New York after London has closed New York", () => {
    expect(zoneOf("2026-06-10T18:00:00+00:00")).toBe("newyork");
  });

  it("calls Tokyo's morning Tokyo", () => {
    expect(zoneOf("2026-06-10T01:00:00+00:00")).toBe("tokyo");
  });

  it("reads a timestamp without an offset as UTC", () => {
    expect(instant("2026-06-10T09:00:00").toISOString()).toBe("2026-06-10T09:00:00.000Z");
  });
});

describe("slicing", () => {
  const all = [
    trade("2026-06-10T09:00:00+00:00"),
    trade("2026-06-11T01:00:00+00:00", { side: "short", net: -4, gross: -2 }),
    trade("2026-06-12T18:00:00+00:00", { exit_reason: "the target was reached" }),
  ];

  it("changes nothing when nothing is chosen", () => {
    expect(apply(all, NO_SLICE)).toBe(all);
  });

  it("combines filters", () => {
    expect(apply(all, { ...NO_SLICE, side: "long", zone: "newyork" })).toHaveLength(1);
    expect(apply(all, { ...NO_SLICE, weekdays: ["Wed"] })).toHaveLength(1);
  });

  it("totals what is left", () => {
    expect(figures(all)).toEqual({ trades: 3, wins: 2, gross: 18, costs: 10, net: 8 });
  });
});
