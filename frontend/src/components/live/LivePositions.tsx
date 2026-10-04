import { useState } from "react";
import type { LiveLeg, LiveSession } from "../../api";
import { num, signed } from "../../format";
import type { Unit } from "./units";
import { istTime, shortDay } from "./units";

interface Props {
  legs: LiveLeg[];
  session: LiveSession | null;
  sessions: LiveSession[];
  unit: Unit;
  onSession: (id: number | null) => void;
  onExit: (id: number) => void;
  onRemove: (id: number) => void;
  onLevels: (id: number, patch: { stop?: number | null; target?: number | null; enabled?: boolean }) => void;
  onRule: (patch: { rule_stop?: number | null; rule_target?: number | null }) => void;
  onExitAll: () => void;
}

const tone = (v: number | null) => (v === null ? "" : v > 0 ? "up" : v < 0 ? "dn" : "");

/** A level as % of the entry, against the position: up for a short's stop. */
function toPct(leg: LiveLeg, level: number | null, against: boolean): number | null {
  if (level === null) return null;
  const sign = (leg.side === "sell") === against ? 1 : -1;
  return Math.round(sign * (level / leg.entry_price - 1) * 1000) / 10;
}

function fromPct(leg: LiveLeg, pct: number | null, against: boolean): number | null {
  if (pct === null) return null;
  const sign = (leg.side === "sell") === against ? 1 : -1;
  return Math.max(0.05, Math.round(leg.entry_price * (1 + (sign * pct) / 100) * 100) / 100);
}

/** Paper legs: filled from the live market, kept and closed by the server. */
export function LivePositions(p: Props) {
  const { unit } = p;
  const open = p.legs.filter((l) => l.status === "open");
  const net = p.legs.reduce((a, l) => a + (l.enabled ? (l.net ?? 0) : 0), 0);
  const fees = p.legs.reduce((a, l) => a + (l.enabled ? l.fees : 0), 0);

  return (
    <section className="sim-pos" aria-label="Positions">
      <header>
        <h3>Positions</h3>
        {p.session?.mode === "live" && <span className="lb-livebadge">LIVE</span>}
        <select
          className="sim-session"
          value={p.session?.id ?? ""}
          onChange={(e) => p.onSession(e.target.value ? Number(e.target.value) : null)}
          aria-label="Paper session"
        >
          <option value="">{p.sessions.length ? "New session" : "No sessions yet"}</option>
          {p.sessions.map((s) => (
            <option key={s.id} value={s.id}>
              {s.name}
            </option>
          ))}
        </select>
        {p.session && <RuleControl session={p.session} unit={unit} onRule={p.onRule} />}
        {open.length > 0 && (
          <button className="sim-exitnow" onClick={p.onExitAll}>
            Exit all
          </button>
        )}
        <span
          className={`sim-total ${tone(net)}`}
          title={`Fees ${signed(-fees, unit.dp)}, an open leg's exit included`}
        >
          Net P&amp;L <b>{signed(net, unit.dp)}</b>
        </span>
      </header>
      {p.session?.squared && (
        <p className={`sim-squared ${tone(p.session.squared.net)}`} role="status">
          Squared off at {istTime(p.session.squared.at)} on the P&amp;L{" "}
          {p.session.squared.reason === "portfolio stop" ? "stop" : "target"} · net{" "}
          {signed(p.session.squared.net, unit.dp)}
        </p>
      )}
      {p.legs.length === 0 ? (
        <p className="sim-empty">
          {p.session ? "Nothing traded in this session yet." : "Paper trade a draft to start one."}
        </p>
      ) : (
        <div className="sim-legs">
          <table>
            <thead>
              <tr>
                <th />
                <th />
                <th>{unit.label}</th>
                <th>Expiry</th>
                <th>Strike</th>
                <th>Type</th>
                <th className="r">Entry</th>
                <th className="r">Bid / ask · exit</th>
                <th className="r" title="After fees, an open leg's exit at the mark included">
                  Net P&amp;L
                </th>
                <th title="Stop and target, % of the entry against and for the position. Judged on the bid or ask that would close it.">
                  SL / TG %
                </th>
                <th />
              </tr>
            </thead>
            <tbody>
              {p.legs.map((l) => {
                const live = l.status === "open";
                return (
                  <tr key={l.id} className={`${l.status}${l.enabled ? "" : " off"}`}>
                    <td>
                      <input
                        type="checkbox"
                        checked={l.enabled}
                        onChange={(e) => p.onLevels(l.id, { enabled: e.target.checked })}
                        aria-label="Include in payoff, total and exit-all"
                      />
                    </td>
                    <td>
                      <span className={`sim-side ${l.side}`}>{l.side === "buy" ? "B" : "S"}</span>
                    </td>
                    <td>{unit.show(l.qty)}</td>
                    <td>{shortDay(l.expiry)}</td>
                    <td>{l.strike}</td>
                    <td>
                      <span className={`sim-kind ${l.kind}`}>{l.kind}</span>
                    </td>
                    <td className="r">
                      {num(l.entry_price)}
                      <small>{istTime(l.entry_at)}</small>
                    </td>
                    <td className="r">
                      {live ? (
                        <>
                          {l.bid != null ? num(l.bid) : "—"} / {l.ask != null ? num(l.ask) : "—"}
                          <small>mark {l.mark != null ? num(l.mark) : "—"}</small>
                        </>
                      ) : (
                        <>
                          {num(l.exit_price ?? 0)}
                          <small>
                            {l.exit_reason} {l.exit_at ? istTime(l.exit_at) : ""}
                          </small>
                        </>
                      )}
                    </td>
                    <td
                      className={`r ${tone(l.net)}`}
                      title={
                        l.gross === null
                          ? undefined
                          : `Gross ${signed(l.gross, unit.dp)} · fees ${signed(-l.fees, unit.dp)}`
                      }
                    >
                      {l.net === null ? "" : signed(l.net, unit.dp)}
                    </td>
                    <td>
                      <div className="sim-levels">
                        <Pct
                          value={toPct(l, l.stop, true)}
                          disabled={!live}
                          label="Stop"
                          onCommit={(v) => p.onLevels(l.id, { stop: fromPct(l, v, true) })}
                        />
                        <Pct
                          value={toPct(l, l.target, false)}
                          disabled={!live}
                          label="Target"
                          onCommit={(v) => p.onLevels(l.id, { target: fromPct(l, v, false) })}
                        />
                      </div>
                    </td>
                    <td>
                      <div className="sim-acts">
                        {live ? (
                          <button onClick={() => p.onExit(l.id)}>Exit</button>
                        ) : (
                          <button onClick={() => p.onRemove(l.id)} aria-label="Remove leg">
                            ✕
                          </button>
                        )}
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

/** A percentage typed and sent when done - on Enter or leaving the box - not per keystroke. */
function Pct({
  value,
  disabled,
  label,
  onCommit,
}: {
  value: number | null;
  disabled: boolean;
  label: string;
  onCommit: (pct: number | null) => void;
}) {
  const [draft, setDraft] = useState<string | null>(null);
  const shown = draft ?? (value === null ? "" : String(value));
  const commit = () => {
    if (draft === null) return;
    const v = draft.trim() === "" ? null : Math.max(0.1, Number(draft));
    setDraft(null);
    if (v !== value && (v === null || Number.isFinite(v))) onCommit(v);
  };
  return (
    <input
      type="number"
      min={1}
      step={5}
      placeholder={label === "Stop" ? "SL" : "TG"}
      value={shown}
      disabled={disabled}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => e.key === "Enter" && commit()}
      aria-label={`${label}, % of entry`}
    />
  );
}

/** Square everything off at a net P&L. Held by the server's watcher. */
function RuleControl({
  session,
  unit,
  onRule,
}: {
  session: LiveSession;
  unit: Unit;
  onRule: Props["onRule"];
}) {
  const [stop, setStop] = useState<string | null>(null);
  const [target, setTarget] = useState<string | null>(null);
  const armed = session.rule_stop !== null || session.rule_target !== null;
  const read = (t: string) => (t.trim() === "" ? null : Math.max(0.01, Number(t)));
  const shown = (v: number | null) => (v === null ? "" : String(Math.round(v * 100) / 100));
  return (
    <div
      className={`sim-exitall${armed ? " on" : ""}`}
      title="Square off every included leg when the net P&L reaches either. Watched by the server."
    >
      <span>Exit all at</span>
      <input
        type="number"
        min={0}
        placeholder="loss"
        value={stop ?? shown(session.rule_stop)}
        onChange={(e) => setStop(e.target.value)}
        onBlur={() => {
          if (stop !== null) onRule({ rule_stop: read(stop) });
          setStop(null);
        }}
        aria-label={`Loss to square off at, ${unit.currency}`}
      />
      <input
        type="number"
        min={0}
        placeholder="profit"
        value={target ?? shown(session.rule_target)}
        onChange={(e) => setTarget(e.target.value)}
        onBlur={() => {
          if (target !== null) onRule({ rule_target: read(target) });
          setTarget(null);
        }}
        aria-label={`Profit to square off at, ${unit.currency}`}
      />
      <span>{unit.money}</span>
    </div>
  );
}
