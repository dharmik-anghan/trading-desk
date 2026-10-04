import { NTH } from "./legs";
import type { LegDraft } from "./legs";

interface Props {
  index: number;
  leg: LegDraft;
  onChange: (leg: LegDraft) => void;
  onCopy: () => void;
  onRemove: (() => void) | null;
  /** A "≈ days out" strategy has one expiry; a leg cannot name the 2nd of it. */
  daysSeries: boolean;
  /** A coin's options trade in fractions of it: each "lot" is one `step`. */
  coin?: { name: string; step: number } | null;
  /** What premiums are priced in. */
  currency?: string;
}

/** ITM 10 … ATM … OTM 10, counted in listed strikes from the money. */
const OFFSETS = Array.from({ length: 21 }, (_, i) => i - 10);

const offsetLabel = (o: number) => (o === 0 ? "ATM" : o > 0 ? `OTM ${o}` : `ITM ${-o}`);

/**
 * One leg, read left to right like the order it stands for:
 * sell · CE · 1 lot · same expiry · ATM · stop 25% · no target.
 */
export function LegRow({
  index,
  leg,
  onChange,
  onCopy,
  onRemove,
  daysSeries,
  coin = null,
  currency = "₹",
}: Props) {
  const set = <K extends keyof LegDraft>(key: K, value: LegDraft[K]) =>
    onChange({ ...leg, [key]: value });

  return (
    <div className={`ob-leg ${leg.side}`} role="group" aria-label={`Leg ${index + 1}`}>
      <span className="ob-n">{index + 1}</span>

      <div className="ob-seg side">
        <button
          className={leg.side === "buy" ? "on" : ""}
          aria-pressed={leg.side === "buy"}
          onClick={() => set("side", "buy")}
        >
          Buy
        </button>
        <button
          className={leg.side === "sell" ? "on" : ""}
          aria-pressed={leg.side === "sell"}
          onClick={() => set("side", "sell")}
        >
          Sell
        </button>
      </div>

      <div className="ob-seg">
        {(["CE", "PE"] as const).map((k) => (
          <button
            key={k}
            className={leg.kind === k ? "on" : ""}
            aria-pressed={leg.kind === k}
            onClick={() => set("kind", k)}
          >
            {k}
          </button>
        ))}
      </div>

      <label
        className="ob-lots"
        title={coin ? `Steps of ${coin.step} ${coin.name}` : "Lots, at the lot size in force on each day"}
      >
        <input
          type="number"
          min={1}
          max={100}
          value={leg.lots}
          onChange={(e) => set("lots", Math.max(1, Number(e.target.value)))}
          aria-label="Lots"
        />
        <span>
          {coin ? `${+(leg.lots * coin.step).toFixed(4)} ${coin.name}` : leg.lots === 1 ? "lot" : "lots"}
        </span>
      </label>

      <select
        className="ob-legexp"
        value={daysSeries ? 0 : leg.expiryNth}
        disabled={daysSeries}
        onChange={(e) => set("expiryNth", Number(e.target.value))}
        aria-label="Expiry"
        title="This leg's own expiry, for a calendar - the nth of the strategy's series"
      >
        <option value={0}>Same expiry</option>
        {NTH.map((label, k) => (
          <option key={label} value={k + 1}>
            {label} expiry
          </option>
        ))}
      </select>

      <div className="ob-strike">
        <select
          value={leg.strikeMode === "atm" ? String(leg.offset) : leg.strikeMode}
          onChange={(e) =>
            /^-?\d+$/.test(e.target.value)
              ? onChange({ ...leg, strikeMode: "atm", offset: Number(e.target.value) })
              : onChange({ ...leg, strikeMode: e.target.value as LegDraft["strikeMode"] })
          }
          aria-label="Strike"
          title="Counted in listed strikes from the money. OTM is above spot for a call, below for a put."
        >
          {OFFSETS.map((o) => (
            <option key={o} value={o}>
              {offsetLabel(o)}
            </option>
          ))}
          <option value="delta">Delta…</option>
          <option value="pct">% from spot…</option>
          <option value="premium">Premium near {currency}…</option>
          <option value="straddle_width">x straddle width…</option>
          <option value="sp_pct">% of straddle premium…</option>
        </select>
        {leg.strikeMode === "delta" && (
          <input
            type="number"
            min={0.01}
            max={0.99}
            step={0.01}
            value={leg.delta}
            onChange={(e) => set("delta", Math.min(0.99, Math.max(0.01, Number(e.target.value))))}
            aria-label="Delta"
            title="Absolute delta, worked out from each strike's own premium"
          />
        )}
        {leg.strikeMode === "pct" && (
          <input
            type="number"
            step={0.5}
            value={leg.pct}
            onChange={(e) => set("pct", Number(e.target.value))}
            aria-label="Percent from spot, OTM positive"
            title="OTM is positive: above spot for a call, below for a put"
          />
        )}
        {leg.strikeMode === "premium" && (
          <input
            type="number"
            min={0}
            step={5}
            value={leg.premium}
            onChange={(e) => set("premium", Math.max(0, Number(e.target.value)))}
            aria-label={`Target premium in ${currency}`}
          />
        )}
        {leg.strikeMode === "straddle_width" && (
          <input
            type="number"
            min={0.1}
            step={0.1}
            value={leg.widthMult}
            onChange={(e) => set("widthMult", Math.max(0.1, Number(e.target.value)))}
            aria-label="Multiple of the ATM straddle's premium"
            title="The ATM call + put premium, times this, away from the ATM strike"
          />
        )}
        {leg.strikeMode === "sp_pct" && (
          <input
            type="number"
            min={1}
            step={5}
            value={leg.spPct}
            onChange={(e) => set("spPct", Math.max(1, Number(e.target.value)))}
            aria-label="Percent of the ATM straddle's premium"
            title="The strike whose own premium is nearest this percent of the ATM straddle's"
          />
        )}
      </div>

      <Level
        name="Stop"
        kind={leg.stopKind}
        value={leg.stopValue}
        onKind={(k) => set("stopKind", k)}
        onValue={(v) => set("stopValue", v)}
      />
      <Level
        name="Target"
        kind={leg.targetKind}
        value={leg.targetValue}
        onKind={(k) => set("targetKind", k)}
        onValue={(v) => set("targetValue", v)}
      />

      <div className="ob-acts">
        <button onClick={onCopy} title="Copy this leg" aria-label={`Copy leg ${index + 1}`}>
          Copy
        </button>
        {onRemove && (
          <button onClick={onRemove} title="Remove this leg" aria-label={`Remove leg ${index + 1}`}>
            ✕
          </button>
        )}
      </div>
    </div>
  );
}

/** A stop or a target: none, a percentage of the premium, or points. */
function Level({
  name,
  kind,
  value,
  onKind,
  onValue,
}: {
  name: string;
  kind: "none" | "pct" | "points";
  value: number;
  onKind: (k: "none" | "pct" | "points") => void;
  onValue: (v: number) => void;
}) {
  return (
    <div className={`ob-level ${kind}`}>
      <span className="ob-lbl">{name}</span>
      {kind !== "none" && (
        <input
          type="number"
          min={0}
          step={kind === "pct" ? 5 : 1}
          value={value}
          onChange={(e) => onValue(Math.max(0, Number(e.target.value)))}
          aria-label={`${name} amount`}
        />
      )}
      <select
        value={kind}
        onChange={(e) => onKind(e.target.value as "none" | "pct" | "points")}
        aria-label={`${name} type`}
      >
        <option value="none">none</option>
        <option value="pct">%</option>
        <option value="points">pts</option>
      </select>
    </div>
  );
}
