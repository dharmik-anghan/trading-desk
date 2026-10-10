import type { Level, LevelKind } from "../../api";

interface Props {
  label: string;
  value: Level | null;
  onChange: (next: Level | null) => void;
  /** A target can be a multiple of the risk; a stop cannot. */
  allowReward?: boolean;
}

const KINDS: { id: LevelKind; name: string }[] = [
  { id: "percent", name: "a percent away" },
  { id: "candle", name: "a candle's price" },
  { id: "atr", name: "an ATR multiple" },
  { id: "pivot", name: "a pivot level" },
  { id: "swing", name: "the last swing low / high" },
  { id: "reward", name: "a multiple of the risk" },
];

/**
 * Where a stop or a target goes.
 *
 * Worked out once, when the position opens, and then left alone - a level that
 * moved every bar would be a trailing stop, which is a different thing and should
 * be asked for by name rather than arrived at by accident.
 */
export function LevelPicker({ label, value, onChange, allowReward = false }: Props) {
  const kinds = allowReward ? KINDS : KINDS.filter((k) => k.id !== "reward");

  return (
    <div className="level">
      <label>{label}</label>
      <select
        value={value?.kind ?? ""}
        onChange={(e) => {
          const kind = e.target.value as LevelKind | "";
          if (!kind) return onChange(null);
          if (kind === "candle") onChange({ kind, field: "low", ago: 1 });
          else if (kind === "atr") onChange({ kind, length: 14, value: 2 });
          else if (kind === "pivot") onChange({ kind, level: "S1" });
          else if (kind === "swing") onChange({ kind, length: 5, value: 2 });
          else if (kind === "reward") onChange({ kind, value: 2 });
          else onChange({ kind: "percent", value: 1 });
        }}
      >
        <option value="">none</option>
        {kinds.map((k) => (
          <option key={k.id} value={k.id}>
            {k.name}
          </option>
        ))}
      </select>

      {value?.kind === "percent" && (
        <>
          <input
            className="num"
            type="number"
            step="0.1"
            value={value.value ?? 1}
            onChange={(e) => onChange({ ...value, value: Number(e.target.value) })}
          />
          <small>%</small>
        </>
      )}

      {value?.kind === "candle" && (
        <>
          <select
            value={value.field ?? "low"}
            onChange={(e) => onChange({ ...value, field: e.target.value })}
          >
            {["low", "high", "close", "open"].map((f) => (
              <option key={f} value={f}>
                {f}
              </option>
            ))}
          </select>
          <input
            className="num"
            type="number"
            min={0}
            value={value.ago ?? 1}
            onChange={(e) => onChange({ ...value, ago: Math.max(0, Number(e.target.value)) })}
            title="Bars back. 1 is the previous candle"
          />
          <small>bars back</small>
        </>
      )}

      {value?.kind === "atr" && (
        <>
          <input
            className="num"
            type="number"
            min={1}
            value={value.length ?? 14}
            onChange={(e) => onChange({ ...value, length: Math.max(1, Number(e.target.value)) })}
            title="ATR period"
          />
          <small>×</small>
          <input
            className="num"
            type="number"
            step="0.1"
            value={value.value ?? 2}
            onChange={(e) => onChange({ ...value, value: Number(e.target.value) })}
          />
        </>
      )}

      {value?.kind === "pivot" && (
        <select
          value={value.level ?? "S1"}
          onChange={(e) => onChange({ ...value, level: e.target.value })}
        >
          {["S3", "S2", "S1", "P", "R1", "R2", "R3"].map((l) => (
            <option key={l} value={l}>
              {l}
            </option>
          ))}
        </select>
      )}

      {value?.kind === "swing" && (
        <>
          <input
            className="num"
            type="number"
            min={1}
            value={value.length ?? 5}
            onChange={(e) => onChange({ ...value, length: Math.max(1, Number(e.target.value)) })}
            title="Bars either side that must be higher (lower for a short)"
          />
          <small>bars each side, skip if wider than</small>
          <input
            className="num"
            type="number"
            min={0}
            step="0.1"
            value={value.value ?? 0}
            onChange={(e) => onChange({ ...value, value: Math.max(0, Number(e.target.value)) })}
            title="Widest stop allowed, as a percent of the entry. 0 for no limit"
          />
          <small>%</small>
        </>
      )}

      {value?.kind === "reward" && (
        <>
          <input
            className="num"
            type="number"
            step="0.5"
            min={0}
            value={value.value ?? 2}
            onChange={(e) => onChange({ ...value, value: Number(e.target.value) })}
          />
          <small>× the risk</small>
        </>
      )}
    </div>
  );
}
