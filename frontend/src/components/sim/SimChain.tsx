import { useEffect, useMemo, useRef } from "react";
import type { SimFetch, SimMoment, SimSide } from "../../api";
import { compact, num } from "../../format";
import type { Leg } from "./legs";

interface Props {
  moment: SimMoment;
  legs: Leg[];
  onTrade: (side: "buy" | "sell", kind: "CE" | "PE", strike: number) => void;
  onExpiry: (expiry: string) => void;
  /** What is being fetched from Fyers for this page, while it is. */
  loading: SimFetch | null;
}

/** A price this much older than the moment is marked as not having traded since. */
const STALE_MINUTES = 15;

const dayLabel = (iso: string) =>
  new Date(`${iso}T00:00:00`).toLocaleDateString("en-IN", {
    day: "2-digit",
    month: "short",
  });

function stale(side: SimSide, at: string): boolean {
  return (new Date(at).getTime() - new Date(side.last_at).getTime()) / 60000 > STALE_MINUTES;
}

/**
 * The chain as it stood at the moment: calls left, puts right, the strike
 * between. In-the-money halves are shaded; the ATM row is marked; a strike you
 * hold says so beside its price. Hovering a row offers B and S on each side.
 */
export function SimChain({ moment, legs, onTrade, onExpiry, loading }: Props) {
  const box = useRef<HTMLDivElement>(null);
  const atmRow = useRef<HTMLTableRowElement>(null);
  const maxOi = useMemo(() => {
    let ce = 1;
    let pe = 1;
    for (const r of moment.rows) {
      ce = Math.max(ce, r.ce?.oi ?? 0);
      pe = Math.max(pe, r.pe?.oi ?? 0);
    }
    return { ce, pe };
  }, [moment.rows]);

  // To the money whenever the expiry changes - not on every step, which
  // would fight anyone scrolling the chain during autoplay.
  useEffect(() => {
    const row = atmRow.current;
    const el = box.current;
    if (row && el) el.scrollTop = row.offsetTop - el.clientHeight / 2 + row.clientHeight / 2;
  }, [moment.expiry]);

  const held = (kind: "CE" | "PE", strike: number) =>
    legs
      .filter(
        (l) => l.kind === kind && l.strike === strike && l.expiry === moment.expiry && l.status === "open",
      )
      .reduce((n, l) => n + (l.side === "buy" ? l.lots : -l.lots), 0);

  const shown = moment.expiries.slice(0, 4);
  const more = moment.expiries.slice(4);

  return (
    <section className="sim-chain" aria-label="Option chain">
      <div className="sim-strip">
        <span>
          Spot <b>{num(moment.spot)}</b>
        </span>
        {moment.vix !== null && (
          <span>
            VIX <b>{num(moment.vix)}</b>
          </span>
        )}
        {moment.future !== null && moment.future_expiry && (
          <span>
            FUT ({dayLabel(moment.future_expiry)}) <b>{num(moment.future)}</b>
          </span>
        )}
        {moment.atm_iv !== null && (
          <span title="Mean implied volatility of the ATM call and put">
            ATM IV <b>{num(moment.atm_iv * 100, 1)}</b>
          </span>
        )}
      </div>
      <div className="sim-expiries" role="tablist" aria-label="Expiry">
        {shown.map((e) => (
          <button
            key={e.expiry}
            role="tab"
            aria-selected={e.expiry === moment.expiry}
            className={e.expiry === moment.expiry ? "on" : ""}
            onClick={() => onExpiry(e.expiry)}
          >
            {dayLabel(e.expiry)} <span>({e.days}d)</span>
          </button>
        ))}
        {more.length > 0 && (
          <select
            value={more.some((e) => e.expiry === moment.expiry) ? (moment.expiry ?? "") : ""}
            onChange={(ev) => ev.target.value && onExpiry(ev.target.value)}
            aria-label="Later expiries"
            className={more.some((e) => e.expiry === moment.expiry) ? "on" : ""}
          >
            <option value="">Later…</option>
            {more.map((e) => (
              <option key={e.expiry} value={e.expiry}>
                {dayLabel(e.expiry)} ({e.days}d){e.monthly ? " · monthly" : ""}
              </option>
            ))}
          </select>
        )}
      </div>
      {loading && <Loading fetch={loading} />}
      <div className="sim-table" ref={box}>
        {moment.rows.length === 0 ? (
          <p className="sim-empty">{loading ? "" : "No contracts of this expiry traded by this moment."}</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>CallΔ</th>
                <th className="r">LTP</th>
                <th className="r">OI</th>
                <th className="k">Strike</th>
                <th>OI</th>
                <th className="r">LTP</th>
                <th className="r">PutΔ</th>
              </tr>
            </thead>
            <tbody>
              {moment.rows.map((r) => {
                const atm = r.strike === moment.atm;
                const ceItm = r.strike < moment.spot;
                const ceHeld = held("CE", r.strike);
                const peHeld = held("PE", r.strike);
                return (
                  <tr key={r.strike} ref={atm ? atmRow : undefined} className={atm ? "atm" : ""}>
                    <td className={ceItm ? "itm" : ""}>{r.ce?.delta != null ? num(r.ce.delta) : ""}</td>
                    <td className={`r ${ceItm ? "itm" : ""}`}>
                      <Price side={r.ce} at={moment.at} />
                    </td>
                    <td className={`oi ce ${ceItm ? "itm" : ""}`}>
                      <Trade kind="CE" strike={r.strike} held={ceHeld} disabled={!r.ce} onTrade={onTrade} />
                      {r.ce && <Bar value={r.ce.oi} max={maxOi.ce} side="ce" />}
                    </td>
                    <td className="k">{r.strike}</td>
                    <td className={`oi pe ${!ceItm ? "itm" : ""}`}>
                      {r.pe && <Bar value={r.pe.oi} max={maxOi.pe} side="pe" />}
                      <Trade kind="PE" strike={r.strike} held={peHeld} disabled={!r.pe} onTrade={onTrade} />
                    </td>
                    <td className={`r ${!ceItm ? "itm" : ""}`}>
                      <Price side={r.pe} at={moment.at} />
                    </td>
                    <td className={`r ${!ceItm ? "itm" : ""}`}>
                      {r.pe?.delta != null ? num(r.pe.delta) : ""}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </section>
  );
}

/** What is being fetched from Fyers for the page, while it is. */
export function Loading({ fetch }: { fetch: SimFetch }) {
  const what = fetch.expiry ? `the ${dayLabel(fetch.expiry)} expiry` : dayLabel(fetch.day);
  const step =
    fetch.state === "fetching"
      ? `${fetch.done} / ${fetch.total} contracts`
      : fetch.state === "index"
        ? "the session"
        : "the expiries";
  const share = fetch.total ? (fetch.done / fetch.total) * 100 : 0;
  return (
    <div className="sim-source" role="status">
      <span>
        Loading {what} from Fyers · {step}
      </span>
      <i style={{ width: `${share}%` }} />
    </div>
  );
}

function Price({ side, at }: { side: SimSide | null; at: string }) {
  if (!side) return null;
  const old = stale(side, at);
  return (
    <span
      className={old ? "stale" : undefined}
      title={old ? `Last traded ${new Date(side.last_at).toLocaleString("en-IN")}` : undefined}
    >
      {num(side.ltp)}
    </span>
  );
}

function Bar({ value, max, side }: { value: number; max: number; side: "ce" | "pe" }) {
  return (
    <span className={`sim-oi ${side}`}>
      <i style={{ width: `${Math.max(2, (value / max) * 100)}%` }} />
      <em>{compact(value)}</em>
    </span>
  );
}

function Trade({
  kind,
  strike,
  held,
  disabled,
  onTrade,
}: {
  kind: "CE" | "PE";
  strike: number;
  held: number;
  disabled: boolean;
  onTrade: Props["onTrade"];
}) {
  return (
    <span className="sim-trade">
      {held !== 0 && (
        <span className={`sim-held ${held > 0 ? "long" : "short"}`} title="Lots held">
          {held > 0 ? `+${held}` : held}
        </span>
      )}
      {!disabled && (
        <span className="sim-bs">
          <button
            className="b"
            onClick={() => onTrade("buy", kind, strike)}
            aria-label={`Buy ${strike} ${kind}`}
          >
            B
          </button>
          <button
            className="s"
            onClick={() => onTrade("sell", kind, strike)}
            aria-label={`Sell ${strike} ${kind}`}
          >
            S
          </button>
        </span>
      )}
    </span>
  );
}
