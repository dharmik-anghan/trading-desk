import type { OptbtCondition, OptbtOperand } from "../../api";

const LEVELS: [OptbtOperand["level"], string][] = [
  ["P", "Pivot"],
  ["R1", "R1"],
  ["R2", "R2"],
  ["R3", "R3"],
  ["S1", "S1"],
  ["S2", "S2"],
  ["S3", "S3"],
  ["PDH", "Prev high"],
  ["PDL", "Prev low"],
  ["PDC", "Prev close"],
  ["DO", "Day open"],
];

const OPS: [OptbtCondition["op"], string][] = [
  ["above", "above"],
  ["below", "below"],
  ["crosses_above", "crosses above"],
  ["crosses_below", "crosses below"],
];

const TIMEFRAMES: OptbtCondition["timeframe"][] = [1, 3, 5, 10, 15, 30, 60];

const operand = (kind: OptbtOperand["kind"], extra: Partial<OptbtOperand> = {}): OptbtOperand => ({
  kind,
  length: kind === "rsi" ? 14 : kind === "supertrend" ? 10 : 20,
  mult: 3,
  level: "P",
  value: kind === "number" ? 50 : 0,
  ...extra,
});

/** Spot above EMA 20 on 5m: where a new condition starts. */
export const newCondition = (): OptbtCondition => ({
  left: operand("price"),
  op: "above",
  right: operand("ema"),
  timeframe: 5,
});

/** An operand as one select value: its kind, or "level:R1". */
const pickOf = (o: OptbtOperand) => (o.kind === "level" ? `level:${o.level}` : o.kind);

function fromPick(pick: string, was: OptbtOperand): OptbtOperand {
  if (pick.startsWith("level:")) return { ...was, kind: "level", level: pick.slice(6) as OptbtOperand["level"] };
  const kind = pick as OptbtOperand["kind"];
  // A fresh default period when the kind changes - RSI 14, not EMA's 20.
  return kind === was.kind ? was : operand(kind, { value: was.value || (kind === "number" ? 50 : 0) });
}

function OperandPick({
  value,
  onChange,
  name,
}: {
  value: OptbtOperand;
  onChange: (o: OptbtOperand) => void;
  name: string;
}) {
  const num = (v: string, min: number) => Math.max(min, Number(v));
  return (
    <span className="ob-operand">
      <select value={pickOf(value)} onChange={(e) => onChange(fromPick(e.target.value, value))} aria-label={name}>
        <option value="price">Spot</option>
        <option value="ema">EMA</option>
        <option value="sma">SMA</option>
        <option value="rsi">RSI</option>
        <option value="supertrend">Supertrend</option>
        <optgroup label="Daily levels">
          {LEVELS.map(([k, label]) => (
            <option key={k} value={`level:${k}`}>
              {label}
            </option>
          ))}
        </optgroup>
        <option value="number">Number</option>
      </select>
      {["ema", "sma", "rsi", "supertrend"].includes(value.kind) && (
        <input
          type="number"
          min={1}
          max={500}
          value={value.length}
          onChange={(e) => onChange({ ...value, length: Math.round(num(e.target.value, 1)) })}
          aria-label={`${name} period`}
          title="Period"
        />
      )}
      {value.kind === "supertrend" && (
        <input
          type="number"
          min={0.5}
          step={0.5}
          value={value.mult}
          onChange={(e) => onChange({ ...value, mult: num(e.target.value, 0.1) })}
          aria-label={`${name} multiplier`}
          title="ATR multiplier"
        />
      )}
      {value.kind === "number" && (
        <input
          type="number"
          step="any"
          value={value.value}
          onChange={(e) => onChange({ ...value, value: Number(e.target.value) })}
          aria-label={`${name} value`}
          className="wide"
        />
      )}
    </span>
  );
}

/** A list of conditions, each read left to right: spot · above · EMA 20 · on 5m. */
export function SignalRows({
  value,
  onChange,
}: {
  value: OptbtCondition[];
  onChange: (next: OptbtCondition[]) => void;
}) {
  const set = (i: number, c: OptbtCondition) => onChange(value.map((x, k) => (k === i ? c : x)));
  return (
    <div className="ob-conds">
      {value.map((c, i) => (
        <div className="ob-cond" key={i} role="group" aria-label={`Condition ${i + 1}`}>
          <OperandPick value={c.left} onChange={(left) => set(i, { ...c, left })} name="Left side" />
          <select
            value={c.op}
            onChange={(e) => set(i, { ...c, op: e.target.value as OptbtCondition["op"] })}
            aria-label="Comparison"
          >
            {OPS.map(([k, label]) => (
              <option key={k} value={k}>
                {label}
              </option>
            ))}
          </select>
          <OperandPick value={c.right} onChange={(right) => set(i, { ...c, right })} name="Right side" />
          <select
            value={c.timeframe}
            onChange={(e) => set(i, { ...c, timeframe: Number(e.target.value) as OptbtCondition["timeframe"] })}
            aria-label="Timeframe"
            title="Read on finished candles of this size"
          >
            {TIMEFRAMES.map((t) => (
              <option key={t} value={t}>
                {t}m
              </option>
            ))}
          </select>
          <button
            className="ob-x"
            onClick={() => onChange(value.filter((_, k) => k !== i))}
            aria-label={`Remove condition ${i + 1}`}
          >
            ✕
          </button>
        </div>
      ))}
      {value.length < 6 && (
        <button className="ob-add" onClick={() => onChange([...value, newCondition()])}>
          + Condition
        </button>
      )}
    </div>
  );
}
