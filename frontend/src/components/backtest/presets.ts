import type { Compare, Operand, StrategySpec } from "../../api";

export interface Preset {
  id: string;
  label: string;
  /** The bar size it was designed for. */
  interval: string;
  spec: StrategySpec;
}

const ema = (length: number, ago = 0, tf?: string): Operand => ({
  kind: "indicator",
  name: "ema",
  length,
  ago,
  tf,
});
const rsi = (tf?: string): Operand => ({ kind: "indicator", name: "rsi", length: 14, tf });
const px = (field: "open" | "high" | "low" | "close", ago = 0): Operand => ({
  kind: "price",
  field,
  ago,
});
const num = (value: number): Operand => ({ kind: "value", value });
const cmp = (left: Operand, op: Compare["op"], right: Operand): Compare => ({ left, op, right });

/** Three candles in a row of one colour: close against open, for the last three. */
const run = (op: "above" | "below"): Compare[] =>
  [0, 1, 2].map((ago) => cmp(px("close", ago), op, px("open", ago)));

/**
 * Strategies worth starting from, as the builder would have made them.
 *
 * Loading one fills the form; nothing about a preset is special once it is
 * loaded. They are starting points, not recommendations - most of them lose after
 * costs on the history held, which the run will say.
 */
export const PRESETS: Preset[] = [
  {
    id: "ema-cross",
    label: "EMA 9/21 crossover",
    interval: "4h",
    spec: {
      name: "EMA 9/21 crossover",
      long_entry: { all: [cmp(ema(9), "crosses_above", ema(21))] },
      long_exit: { all: [cmp(ema(9), "crosses_below", ema(21))] },
      short_entry: null,
      short_exit: null,
      stop: null,
      target: null,
    },
  },
  {
    id: "pullback",
    label: "Trend pullback · EMA 50/200 + RSI",
    interval: "15m",
    spec: {
      name: "Trend pullback · EMA 50/200 + RSI",
      trigger: { kind: "break", field: "high", ago: 0, within: 2 },
      long_entry: {
        all: [
          cmp(ema(50), "above", ema(200)),
          cmp(ema(50), "above", ema(50, 10)),
          cmp(ema(200), "above", ema(200, 10)),
          cmp(px("close"), "above", ema(50)),
          cmp(px("low", 1), "below", ema(50, 1)),
          cmp(px("close", 1), "below", px("open", 1)),
          cmp(px("close"), "above", px("open")),
          cmp(rsi(), "above", num(45)),
          cmp(rsi(), "below", num(55)),
        ],
      },
      short_entry: {
        all: [
          cmp(ema(50), "below", ema(200)),
          cmp(ema(50), "below", ema(50, 10)),
          cmp(ema(200), "below", ema(200, 10)),
          cmp(px("close"), "below", ema(50)),
          cmp(px("high", 1), "above", ema(50, 1)),
          cmp(px("close", 1), "above", px("open", 1)),
          cmp(px("close"), "below", px("open")),
          cmp(rsi(), "above", num(45)),
          cmp(rsi(), "below", num(55)),
        ],
      },
      long_exit: { any: [cmp(rsi(), "above", num(70)), cmp(rsi(), "below", num(45))] },
      short_exit: { any: [cmp(rsi(), "below", num(30)), cmp(rsi(), "above", num(55))] },
      stop: { kind: "swing", length: 5, value: 2 },
      target: null,
    },
  },
  {
    id: "ema-rsi",
    label: "EMA 9/21 cross + 1h RSI",
    interval: "15m",
    spec: {
      name: "EMA 9/21 cross + 1h RSI",
      long_entry: {
        all: [cmp(ema(9), "crosses_above", ema(21)), cmp(rsi("1h"), "above", num(60))],
      },
      short_entry: {
        all: [cmp(ema(9), "crosses_below", ema(21)), cmp(rsi("1h"), "below", num(40))],
      },
      long_exit: { all: [cmp(px("close"), "below", ema(21))] },
      short_exit: { all: [cmp(px("close"), "above", ema(21))] },
      stop: null,
      target: null,
    },
  },
  {
    id: "five-ema-short",
    label: "5 EMA reversal · short",
    interval: "5m",
    spec: {
      name: "5 EMA reversal · short",
      trigger: { kind: "break", field: "high", ago: 0, within: 3 },
      short_entry: { all: [cmp(px("low"), "above", ema(5))] },
      long_entry: null,
      long_exit: null,
      short_exit: null,
      stop: { kind: "candle_mid", ago: 0, value: 1.5, length: 14 },
      target: { kind: "reward", value: 3 },
    },
  },
  {
    id: "trend-ema9-long",
    label: "50 EMA trend + 9 EMA · long",
    interval: "1h",
    spec: {
      name: "50 EMA trend + 9 EMA · long",
      trigger: { kind: "break", field: "high", ago: 0, within: 1 },
      long_entry: {
        all: [cmp(px("close"), "above", ema(50)), cmp(px("close"), "crosses_above", ema(9))],
      },
      long_exit: { all: [cmp(px("close"), "below", ema(9))] },
      short_entry: null,
      short_exit: null,
      stop: { kind: "candle", field: "low", ago: 0 },
      target: null,
    },
  },
  {
    id: "three-candles",
    label: "Three candles in a row",
    interval: "4h",
    spec: {
      name: "Three candles in a row",
      long_entry: { all: run("above") },
      short_entry: { all: run("below") },
      long_exit: null,
      short_exit: null,
      stop: { kind: "candle", field: "far", ago: 2 },
      target: { kind: "reward", value: 3 },
    },
  },
];
