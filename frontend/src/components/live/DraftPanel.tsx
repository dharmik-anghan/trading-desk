import type { DraftLeg, LivePreview, LiveRule, OptbtLevel } from "../../api";
import { num, signed } from "../../format";
import type { Unit } from "./units";
import { shortDay } from "./units";

interface Props {
  legs: DraftLeg[];
  rule: LiveRule | null;
  preview: LivePreview | null;
  unit: Unit;
  /** Units a new leg is added with. */
  size: number;
  step: number;
  onSize: (qty: number) => void;
  onChange: (key: string, patch: Partial<DraftLeg>) => void;
  /** Move a leg one listed strike up (+1) or down (-1); null when it cannot. */
  onNudge: ((key: string, by: 1 | -1) => void) | null;
  onRemove: (key: string) => void;
  onClear: () => void;
  onTrade: () => void;
  trading: boolean;
  /** Why trading is not possible now, if it is not. */
  blocked: string | null;
  mode: "paper" | "live";
  /** Whether this market can be traded for real now. */
  canLive: boolean;
  onMode: (mode: "paper" | "live") => void;
}

/** Quantities are typed in order steps: lots on the NSE, 0.01 BTC on Shark. */
const steps = (qty: number, step: number) => Math.round(qty / step);
const fromSteps = (typed: string, step: number) => Math.max(1, Math.round(Number(typed) || 1)) * step;

const level = (l: OptbtLevel | null, word: string) =>
  l === null ? null : `${word} ${l.kind === "pct" ? `${Math.round(l.value * 100)}%` : `${l.value} pts`}`;

/** The legs to be traded, as they would fill now - before anything is sent. */
export function DraftPanel(p: Props) {
  const { unit } = p;
  const ready = p.legs.filter((l) => l.symbol && !l.error);
  const failed = p.legs.filter((l) => l.error);
  const rule = p.rule ? describeRule(p.rule, unit) : null;
  return (
    <section className="lb-draft" aria-label="Draft">
      <header>
        <h3>Draft</h3>
        {p.canLive && (
          <div className="ob-seg lb-mode" aria-label="Paper or live">
            <button className={p.mode === "paper" ? "on" : ""} onClick={() => p.onMode("paper")}>
              Paper
            </button>
            <button
              className={p.mode === "live" ? "on live" : ""}
              onClick={() => p.onMode("live")}
              title="Real orders on the venue, confirmed first"
            >
              Live
            </button>
          </div>
        )}
        <label className="sim-mult" title={`What a leg added from the chain starts at`}>
          <span>Size</span>
          <input
            type="number"
            min={1}
            step={1}
            value={steps(p.size, p.step)}
            onChange={(e) => p.onSize(fromSteps(e.target.value, p.step))}
            aria-label="Size of a new leg"
          />
          <span>{unit.per === 1 ? `× ${p.step} ${unit.label}` : "lots"}</span>
        </label>
        {rule && (
          <span className="lb-rule" title="Set on the session once the draft is traded">
            {rule}
          </span>
        )}
        {p.legs.length > 0 && (
          <button className="sim-new" onClick={p.onClear}>
            Clear
          </button>
        )}
      </header>
      {p.legs.length === 0 ? (
        <p className="sim-empty">Pick a template, or B / S on the chain.</p>
      ) : (
        <table className="lb-legs">
          <tbody>
            {p.legs.map((l) => (
              <tr key={l.key} className={l.error ? "err" : ""}>
                <td>
                  <button
                    className={`sim-side ${l.side}`}
                    onClick={() => p.onChange(l.key, { side: l.side === "buy" ? "sell" : "buy" })}
                    title="Flip"
                  >
                    {l.side === "buy" ? "B" : "S"}
                  </button>
                </td>
                <td>
                  <input
                    className="sim-lots"
                    type="number"
                    min={1}
                    value={steps(l.qty, p.step)}
                    onChange={(e) => p.onChange(l.key, { qty: fromSteps(e.target.value, p.step) })}
                    aria-label={unit.per === 1 ? `Steps of ${p.step}` : "Lots"}
                    title={unit.per === 1 ? `${unit.show(l.qty)} ${unit.label}` : undefined}
                  />
                </td>
                <td>{l.expiry ? shortDay(l.expiry) : "—"}</td>
                <td>
                  <div className="sim-strike">
                    {p.onNudge && l.strike !== null && (
                      <button onClick={() => p.onNudge?.(l.key, -1)} aria-label="Lower strike">
                        −
                      </button>
                    )}
                    <span>{l.strike ?? "—"}</span>
                    {p.onNudge && l.strike !== null && (
                      <button onClick={() => p.onNudge?.(l.key, 1)} aria-label="Higher strike">
                        +
                      </button>
                    )}
                  </div>
                </td>
                <td>
                  <span className={`sim-kind ${l.kind}`}>{l.kind}</span>
                </td>
                <td className="r">
                  {l.error ? (
                    <span className="dn">{l.error}</span>
                  ) : (
                    <>
                      {l.bid != null ? num(l.bid) : "—"} / {l.ask != null ? num(l.ask) : "—"}
                      {p.preview?.problems[l.symbol ?? ""] && (
                        <small className="dn">{p.preview.problems[l.symbol ?? ""]}</small>
                      )}
                    </>
                  )}
                </td>
                <td className="lb-lv">
                  {[level(l.stop, "SL"), level(l.target, "TG")].filter(Boolean).join(" · ")}
                </td>
                <td>
                  <div className="sim-acts">
                    <button onClick={() => p.onRemove(l.key)} aria-label="Remove from draft">
                      ✕
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {p.legs.length > 0 && (
        <footer>
          {p.preview && (
            <span className="lb-sum">
              {p.preview.premium >= 0 ? "Credit" : "Debit"} <b>{num(Math.abs(p.preview.premium), unit.dp)}</b>{" "}
              · fees {num(p.preview.fees, unit.dp)}
              {p.preview.margin > 0 && <> · margin ≈ {num(p.preview.margin, 0)}</>}
            </span>
          )}
          {failed.length > 0 && <span className="dn">{failed.length} leg could not be placed</span>}
          {p.blocked && <span className="dim">{p.blocked}</span>}
          <button
            className={`ob-run${p.mode === "live" ? " live" : ""}`}
            onClick={p.onTrade}
            disabled={p.trading || ready.length === 0 || failed.length > 0 || p.blocked !== null}
          >
            {p.trading ? "Placing…" : p.mode === "live" ? "Place live order" : "Paper trade"}
          </button>
        </footer>
      )}
    </section>
  );
}

function describeRule(r: LiveRule, unit: Unit): string | null {
  const parts = [];
  if (r.target_credit !== null) parts.push(`take ${Math.round(r.target_credit * 100)}% of credit`);
  if (r.stop_credit !== null) parts.push(`stop at ${Math.round(r.stop_credit * 100)}% of credit`);
  if (r.mtm_target !== null) parts.push(`take ${signed(r.mtm_target, unit.dp)}`);
  if (r.mtm_stop !== null) parts.push(`stop at ${signed(-r.mtm_stop, unit.dp)}`);
  return parts.length ? `Exit all: ${parts.join(", ")}` : null;
}
