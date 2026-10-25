import { useState } from "react";
import { checkBacktest } from "../../api";
import type { BacktestRequest, RealityCheck as Check } from "../../api";

const pct = (v: number) => `${(v * 100).toFixed(0)}%`;

const VERDICT: Record<Check["verdict"], { label: string; tone: string; title: string }> = {
  pass: {
    label: "Real",
    tone: "up",
    title: "Beat random entries and held up in both periods and at double costs",
  },
  weak: {
    label: "Weak",
    tone: "mid",
    title: "Beat random entries, but failed at least one other check",
  },
  fail: {
    label: "Not shown",
    tone: "dn",
    title: "Did not beat random entries. The profit, if any, could be the market or luck",
  },
};

/**
 * Whether the run's result is the strategy or the market and luck.
 *
 * Runs on demand because it is 200 more backtests. The request is the one the
 * result on screen was run with, so editing the form afterwards does not change
 * what is being judged.
 */
export function RealityCheck({ request }: { request: BacktestRequest }) {
  const [check, setCheck] = useState<Check | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const go = () => {
    setBusy(true);
    setError(null);
    checkBacktest(request)
      .then(setCheck)
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setBusy(false));
  };

  return (
    <section className="check">
      <div className="head">
        <h3>Is it real?</h3>
        {check ? (
          <b className={`verdict ${VERDICT[check.verdict].tone}`} title={VERDICT[check.verdict].title}>
            {VERDICT[check.verdict].label}
          </b>
        ) : (
          <button type="button" onClick={go} disabled={busy}>
            {busy ? "Checking…" : "Check against random entries"}
          </button>
        )}
      </div>
      {error && <p className="bad">{error}</p>}
      {check && (
        <>
          <table className="where">
            <tbody>
              {check.checks.map((c) => (
                <tr key={c.name}>
                  <th>{c.name}</th>
                  <td className={c.passed ? "up" : "dn"}>{c.passed ? "yes" : "no"}</td>
                  <td className="say">{c.detail}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="endings">
            Random entries, same exits: median {pct(check.random_median)}, best{" "}
            {pct(check.random_best)}, about {check.random_trades.toLocaleString()} trades each.
            This run: {pct(check.total_return)} on {check.trades.toLocaleString()}. p ={" "}
            {check.p_value.toFixed(3)}.
          </p>
        </>
      )}
    </section>
  );
}
