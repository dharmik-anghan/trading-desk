import type { ExitAll, SimCosts, SimMoment } from "../../api";
import { compact, num, signed } from "../../format";
import type { Leg } from "./legs";
import { legNet, legPnl, levelFrom, levelPct } from "./legs";

interface Props {
  moment: SimMoment;
  legs: Leg[];
  multiplier: number;
  step: number;
  onMultiplier: (n: number) => void;
  /** A change to one leg's own settings. */
  onChange: (id: string, patch: Partial<Leg>) => void;
  /** The leg on another contract: refilled at the moment's price. */
  onRetrade: (id: string, patch: Partial<Leg>) => void;
  onExit: (id: string) => void;
  onReenter: (id: string) => void;
  onRemove: (id: string) => void;
  onToggleAll: (on: boolean) => void;
  /** Square everything off at a net P&L, in rupees or percent of margin. */
  exitAll: ExitAll;
  onExitAll: (rule: ExitAll) => void;
  costs: SimCosts;
  onCosts: (costs: SimCosts) => void;
  /** The last square-off by that rule. */
  squared: SimMoment["squared"];
}

const dayLabel = (iso: string) =>
  new Date(`${iso}T00:00:00`).toLocaleDateString("en-IN", {
    day: "2-digit",
    month: "short",
  });

const clock = (iso: string) => iso.slice(11, 16);

const tone = (v: number | null) => (v === null ? "" : v > 0 ? "up" : v < 0 ? "dn" : "");

/** Every leg traded in this session, open or closed, with what it has made. */
export function SimPositions(props: Props) {
  const { moment, legs, multiplier } = props;
  const gross = legs.reduce((a, l) => a + (l.enabled ? (legPnl(l, multiplier) ?? 0) : 0), 0);
  const charges = legs.reduce(
    (a, l) => a + (l.enabled && legPnl(l, multiplier) !== null ? (l.charges ?? 0) : 0),
    0,
  );
  const total = gross - charges;
  const qty = legs
    .filter((l) => l.enabled && l.status === "open")
    .reduce((a, l) => a + (l.side === "buy" ? 1 : -1) * l.lots * (l.lot_size ?? 0) * multiplier, 0);
  const allOn = legs.length > 0 && legs.every((l) => l.enabled);
  const expiries = moment.expiries.map((e) => e.expiry);

  return (
    <section className="sim-pos" aria-label="Positions">
      <header>
        <h3>Positions</h3>
        <label className="sim-mult" title="Every leg's lots, multiplied">
          <span>Multiplier</span>
          <button onClick={() => props.onMultiplier(Math.max(1, multiplier - 1))} aria-label="Fewer">
            −
          </button>
          <input
            type="number"
            min={1}
            max={100}
            value={multiplier}
            onChange={(e) =>
              props.onMultiplier(Math.max(1, Math.min(100, Math.round(Number(e.target.value)))))
            }
          />
          <button onClick={() => props.onMultiplier(Math.min(100, multiplier + 1))} aria-label="More">
            +
          </button>
        </label>
        <span className="sim-qty">Net qty {signed(qty)}</span>
        <ExitAllControl
          value={props.exitAll}
          onChange={props.onExitAll}
          margin={moment.payoff.span + moment.payoff.exposure}
        />
        <Costs value={props.costs} onChange={props.onCosts} />
        <span
          className={`sim-total ${tone(total)}`}
          title={`Gross ${signed(gross)} · charges and slippage ${signed(-charges)}`}
        >
          Net P&amp;L <b>{signed(total)}</b>
        </span>
      </header>
      {props.squared && (
        <p className={`sim-squared ${tone(props.squared.net)}`} role="status">
          Squared off at {clock(props.squared.at)} on the P&amp;L{" "}
          {props.squared.reason === "portfolio stop" ? "stop" : "target"} · net {signed(props.squared.net)}
        </p>
      )}
      {legs.length === 0 ? (
        <p className="sim-empty">Hover a strike in the chain and press B or S.</p>
      ) : (
        <div className="sim-legs">
          <table>
            <thead>
              <tr>
                <th>
                  <input
                    type="checkbox"
                    checked={allOn}
                    onChange={(e) => props.onToggleAll(e.target.checked)}
                    aria-label="Include every leg"
                  />
                </th>
                <th />
                <th>Lots</th>
                <th>Expiry</th>
                <th>Strike</th>
                <th>Type</th>
                <th className="r">Entry</th>
                <th className="r">LTP / exit</th>
                <th className="r" title="After charges and slippage, an open leg's exit included">
                  Net P&amp;L
                </th>
                <th title="Stop and target, % of the entry price against and for the position">SL / TG %</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {legs.map((l) => {
                const pnl = legNet(l, multiplier);
                const legGross = legPnl(l, multiplier);
                const open = l.status === "open" || l.status === "pending";
                const editable = open && l.entry_price !== null;
                return (
                  <tr key={l.id} className={`${l.status ?? ""}${l.enabled ? "" : " off"}`}>
                    <td>
                      <input
                        type="checkbox"
                        checked={l.enabled}
                        onChange={(e) => props.onChange(l.id, { enabled: e.target.checked })}
                        aria-label="Include in payoff and total"
                      />
                    </td>
                    <td>
                      <button
                        className={`sim-side ${l.side}`}
                        disabled={!open}
                        onClick={() =>
                          props.onRetrade(l.id, {
                            side: l.side === "buy" ? "sell" : "buy",
                          })
                        }
                        title={open ? "Flip: re-trades at the price now" : undefined}
                      >
                        {l.side === "buy" ? "B" : "S"}
                      </button>
                    </td>
                    <td>
                      <input
                        className="sim-lots"
                        type="number"
                        min={1}
                        value={l.lots}
                        disabled={!open}
                        onChange={(e) =>
                          props.onChange(l.id, {
                            lots: Math.max(1, Math.round(Number(e.target.value))),
                          })
                        }
                        aria-label="Lots"
                      />
                    </td>
                    <td>
                      {open ? (
                        <select
                          value={l.expiry}
                          onChange={(e) => props.onRetrade(l.id, { expiry: e.target.value })}
                          aria-label="Expiry"
                        >
                          {[...new Set([l.expiry, ...expiries])].map((e) => (
                            <option key={e} value={e}>
                              {dayLabel(e)}
                            </option>
                          ))}
                        </select>
                      ) : (
                        dayLabel(l.expiry)
                      )}
                    </td>
                    <td>
                      <div className="sim-strike">
                        {open && (
                          <button
                            onClick={() =>
                              props.onRetrade(l.id, {
                                strike: l.strike - props.step,
                              })
                            }
                            aria-label="Lower strike"
                          >
                            −
                          </button>
                        )}
                        <span>{l.strike}</span>
                        {open && (
                          <button
                            onClick={() =>
                              props.onRetrade(l.id, {
                                strike: l.strike + props.step,
                              })
                            }
                            aria-label="Higher strike"
                          >
                            +
                          </button>
                        )}
                      </div>
                    </td>
                    <td>
                      <span className={`sim-kind ${l.kind}`}>{l.kind}</span>
                    </td>
                    <td className="r" title={`Entered ${l.entry_at.replace("T", " ").slice(0, 16)}`}>
                      {l.entry_price !== null ? num(l.entry_price) : "…"}
                      <small>{clock(l.entry_at)}</small>
                    </td>
                    <td className="r">
                      {l.status === "error" ? (
                        <span className="dn">{l.error}</span>
                      ) : l.status === "pending" ? (
                        <span className="dim">from {clock(l.entry_at)}</span>
                      ) : l.status === "closed" ? (
                        <>
                          {num(l.exit_price ?? 0)}
                          <small>
                            {l.exit_reason} {l.exit_at ? clock(l.exit_at) : ""}
                          </small>
                        </>
                      ) : l.ltp != null ? (
                        num(l.ltp)
                      ) : (
                        "…"
                      )}
                    </td>
                    <td
                      className={`r ${tone(pnl)}`}
                      title={
                        legGross === null
                          ? undefined
                          : `Gross ${signed(legGross)} · charges ${signed(-((l.charges ?? 0) - (l.slippage ?? 0)))} · slippage ${signed(-(l.slippage ?? 0))}`
                      }
                    >
                      {pnl === null ? "" : signed(pnl)}
                    </td>
                    <td>
                      <div className="sim-levels">
                        <Pct
                          value={levelPct(l, l.stop, true)}
                          disabled={!editable}
                          label="Stop"
                          onChange={(p) =>
                            props.onChange(l.id, {
                              stop: levelFrom(l, p, true),
                            })
                          }
                        />
                        <Pct
                          value={levelPct(l, l.target, false)}
                          disabled={!editable}
                          label="Target"
                          onChange={(p) =>
                            props.onChange(l.id, {
                              target: levelFrom(l, p, false),
                            })
                          }
                        />
                      </div>
                    </td>
                    <td>
                      <div className="sim-acts">
                        {open ? (
                          <button onClick={() => props.onExit(l.id)} disabled={l.status === "pending"}>
                            Exit
                          </button>
                        ) : (
                          <button
                            onClick={() => props.onReenter(l.id)}
                            title="The same contract again, at the price now"
                          >
                            Re-enter
                          </button>
                        )}
                        <button onClick={() => props.onRemove(l.id)} aria-label="Remove leg">
                          ✕
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function Pct({
  value,
  disabled,
  label,
  onChange,
}: {
  value: number | null;
  disabled: boolean;
  label: string;
  onChange: (pct: number | null) => void;
}) {
  return (
    <input
      type="number"
      min={1}
      step={5}
      placeholder={label === "Stop" ? "SL" : "TG"}
      value={value ?? ""}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value === "" ? null : Math.max(0.1, Number(e.target.value)))}
      aria-label={`${label}, % of entry`}
    />
  );
}

/** Square everything off at a net P&L: a loss and a profit, in rupees or % of margin. */
function ExitAllControl({
  value,
  onChange,
  margin,
}: {
  value: ExitAll;
  onChange: (rule: ExitAll) => void;
  margin: number;
}) {
  const read = (t: string) => (t === "" ? null : Math.max(0, Number(t)));
  const inRs = (v: number | null) =>
    v === null || value.unit === "rs" ? null : ((value.base ?? margin) * v) / 100;
  const hint = (v: number | null) => {
    const rs = inRs(v);
    return rs === null ? undefined : `₹${compact(rs)} of a ₹${compact(value.base ?? margin)} margin`;
  };
  const armed = value.stop !== null || value.target !== null;
  return (
    <div
      className={`sim-exitall${armed ? " on" : ""}`}
      title="Square off every included leg when the net P&L reaches either"
    >
      <span>Exit all at</span>
      <input
        type="number"
        min={0}
        placeholder="loss"
        value={value.stop ?? ""}
        onChange={(e) => onChange({ ...value, stop: read(e.target.value) })}
        aria-label="Loss to square off at"
        title={hint(value.stop)}
      />
      <input
        type="number"
        min={0}
        placeholder="profit"
        value={value.target ?? ""}
        onChange={(e) => onChange({ ...value, target: read(e.target.value) })}
        aria-label="Profit to square off at"
        title={hint(value.target)}
      />
      <select
        value={value.unit}
        onChange={(e) => onChange({ ...value, unit: e.target.value as ExitAll["unit"] })}
        aria-label="In rupees or percent of margin"
      >
        <option value="rs">₹</option>
        <option value="pct">% margin</option>
      </select>
    </div>
  );
}

/** Slippage and brokerage; the taxes are the statutory ones for each day. */
function Costs({ value, onChange }: { value: SimCosts; onChange: (c: SimCosts) => void }) {
  return (
    <details className="sim-costs">
      <summary title="Charges and slippage taken off every fill">
        Costs · {(value.slippage * 100).toFixed(2)}% · ₹{value.brokerage}
      </summary>
      <div>
        <label>
          <span>Slippage, % of premium</span>
          <input
            type="number"
            min={0}
            step={0.05}
            value={Number((value.slippage * 100).toFixed(3))}
            onChange={(e) => onChange({ ...value, slippage: Math.max(0, Number(e.target.value)) / 100 })}
          />
        </label>
        <label>
          <span>At least, ₹</span>
          <input
            type="number"
            min={0}
            step={0.05}
            value={value.min_slip}
            onChange={(e) => onChange({ ...value, min_slip: Math.max(0, Number(e.target.value)) })}
          />
        </label>
        <label>
          <span>Brokerage, ₹ an order</span>
          <input
            type="number"
            min={0}
            value={value.brokerage}
            onChange={(e) => onChange({ ...value, brokerage: Math.max(0, Number(e.target.value)) })}
          />
        </label>
      </div>
    </details>
  );
}
