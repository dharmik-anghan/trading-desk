import { useCallback, useEffect, useRef, useState } from "react";
import {
  exitAllLive,
  exitLiveLeg,
  getLiveMarkets,
  getLiveSessions,
  getLiveState,
  getStrategies,
  getStrategyTemplates,
  patchLiveLeg,
  patchLiveSession,
  placeLive,
  previewLive,
  removeLiveLeg,
  resolveLive,
} from "../api";
import type {
  DraftLeg,
  LiveAccount,
  LiveMarket,
  LivePreview,
  LiveRule,
  LiveSession,
  LiveState,
  OptionStrategySpec,
  SavedStrategy,
  SimMoment,
  SimPayoff as SimPayoffData,
  StrategyTemplate,
} from "../api";
import { num } from "../format";
import { BackButton } from "./BackButton";
import { ConfirmDialog } from "./ConfirmDialog";
import { DraftPanel } from "./live/DraftPanel";
import { LivePositions } from "./live/LivePositions";
import { unitFor } from "./live/units";
import { SimChain } from "./sim/SimChain";
import { SimPayoff } from "./sim/SimPayoff";

interface Props {
  onHome: () => void;
}

/** How often the page reads the market. */
const POLL_MS = 1000;
/** No answer for this long, and the page says it is not live. */
const STALE_MS = 5000;

const sleep = (ms: number) => new Promise((r) => window.setTimeout(r, ms));

let seq = 0;
const key = () => `d${Date.now().toString(36)}${(seq++).toString(36)}`;

const EMPTY: SimPayoffData = {
  pnl: 0,
  realised: 0,
  expiry_curve: [],
  today_curve: [],
  max_profit: null,
  max_loss: null,
  profit_unlimited: false,
  loss_unlimited: false,
  breakevens: [],
  pop: null,
  sd: [],
  span: 0,
  exposure: 0,
  charges: 0,
  net: 0,
};

/** The live state as the simulator's moment, so its chain and payoff draw it. */
function asMoment(s: LiveState, payoff: SimPayoffData | null, margin: number): SimMoment {
  return {
    at: s.at,
    first: s.at,
    last: s.at,
    spot: s.spot,
    vix: null,
    // The forward stands where the replay shows a future: it is the price this
    // expiry is priced off.
    future_expiry: s.expiry,
    future: s.forward,
    expiries: s.expiries,
    expiry: s.expiry,
    lot_size: null,
    atm: s.atm,
    atm_iv: s.atm_iv,
    rows: s.rows,
    legs: s.legs.map((l) => ({
      id: String(l.id),
      side: l.side,
      kind: l.kind,
      strike: l.strike,
      expiry: l.expiry,
      lots: Math.round(l.qty / s.step),
      entry_at: l.entry_at,
      entry_price: l.entry_price,
      stop: l.stop,
      target: l.target,
      exit_at: l.exit_at,
      exit_price: l.exit_price,
      exit_reason: l.exit_reason,
      enabled: l.enabled,
      status: l.status,
      lot_size: null,
      ltp: l.mark,
      ltp_at: s.at,
      iv: l.iv,
      error: null,
      charges: l.fees,
      slippage: 0,
    })),
    payoff: payoff ? { ...payoff, span: margin, exposure: 0 } : EMPTY,
    loading: null,
    squared: s.session?.squared ?? null,
  };
}

/**
 * A strategy on the live chain, NSE or crypto: a template or a saved strategy
 * resolved into contracts now, adjusted, seen as a payoff, then paper traded.
 * The trades are the server's - stops, targets, the exit-all rule and expiry
 * are watched whether or not this page is open.
 */
export function LiveBuilder({ onHome }: Props) {
  const [markets, setMarkets] = useState<LiveMarket[]>([]);
  const [underlying, setUnderlying] = useState("NIFTY");
  const [expiry, setExpiry] = useState("");
  const [state, setState] = useState<LiveState | null>(null);
  const [sessions, setSessions] = useState<LiveSession[]>([]);
  const [sessionId, setSessionId] = useState<number | null>(null);
  const [templates, setTemplates] = useState<StrategyTemplate[]>([]);
  const [saved, setSaved] = useState<SavedStrategy[]>([]);
  const [draft, setDraft] = useState<DraftLeg[]>([]);
  const [rule, setRule] = useState<LiveRule | null>(null);
  const [preview, setPreview] = useState<LivePreview | null>(null);
  const [size, setSize] = useState<number | null>(null);
  const [trading, setTrading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastOk, setLastOk] = useState(0);
  const [now, setNow] = useState(0);
  /** Paper, or real orders on the venue - where the market offers them. */
  const [mode, setMode] = useState<"paper" | "live">("paper");
  /** A real order waiting to be confirmed. */
  const [ask, setAsk] = useState<
    { kind: "basket" } | { kind: "exit"; id: number } | { kind: "exitAll" } | null
  >(null);

  const want = useRef({ underlying, expiry, sessionId });
  useEffect(() => {
    want.current = { underlying, expiry, sessionId };
  }, [underlying, expiry, sessionId]);

  const refresh = useCallback(async () => {
    const w = want.current;
    try {
      const s = await getLiveState({ underlying: w.underlying, expiry: w.expiry, sessionId: w.sessionId });
      const c = want.current;
      // An answer to a question no longer asked is dropped.
      if (c.underlying !== w.underlying || c.expiry !== w.expiry || c.sessionId !== w.sessionId) return;
      setState(s);
      setLastOk(Date.now());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  // One read at a time, a second apart: never two in flight.
  useEffect(() => {
    let alive = true;
    void (async () => {
      while (alive) {
        await refresh();
        setNow(Date.now());
        await sleep(POLL_MS);
      }
    })();
    return () => {
      alive = false;
    };
  }, [refresh]);

  useEffect(() => {
    void getLiveMarkets()
      .then(setMarkets)
      .catch(() => setMarkets([]));
    void getStrategyTemplates()
      .then(setTemplates)
      .catch(() => setTemplates([]));
    void getStrategies()
      .then(setSaved)
      .catch(() => setSaved([]));
  }, []);

  // Each underlying, in each mode, reopens its latest session.
  useEffect(() => {
    void getLiveSessions()
      .then((all) => {
        setSessions(all);
        const latest = all.find((s) => s.underlying === underlying && s.mode === mode)?.id ?? null;
        setSessionId(latest);
        want.current = { ...want.current, sessionId: latest };
      })
      .catch(() => setSessions([]));
  }, [underlying, mode]);

  // What the draft would be, filled now - asked again as it changes, and as the
  // market moves, every few seconds.
  const tradable = draft.filter((d) => d.symbol && !d.error);
  const draftKey = JSON.stringify(tradable.map((d) => [d.symbol, d.side, d.qty]));
  useEffect(() => {
    if (!tradable.length) {
      setPreview(null);
      return;
    }
    let alive = true;
    const ask = () =>
      previewLive(
        underlying,
        tradable.map((d) => ({ symbol: d.symbol as string, side: d.side, qty: d.qty })),
      )
        .then((p) => alive && setPreview(p))
        .catch(() => undefined);
    void ask();
    const timer = window.setInterval(() => void ask(), 3000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
    // The key stands for the draft's tradable legs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draftKey, underlying]);

  const act = (p: Promise<unknown>) => {
    void p.then(() => refresh()).catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
  };

  const pick = (next: string) => {
    setUnderlying(next);
    setExpiry("");
    setState(null);
    setDraft([]);
    setRule(null);
    setSize(null);
    want.current = { underlying: next, expiry: "", sessionId: null };
  };

  const load = (spec: OptionStrategySpec) => {
    setError(null);
    void resolveLive(underlying, spec)
      .then((r) => {
        setDraft(r.legs.map((l) => ({ ...l, key: key() })));
        const any = Object.values(r.rule).some((v) => v !== null);
        setRule(any ? r.rule : null);
      })
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
  };

  const step = state?.step ?? 1;
  const legSize = size ?? state?.min_qty ?? step;

  const addFromChain = (side: "buy" | "sell", kind: "CE" | "PE", strike: number) => {
    const row = state?.rows.find((r) => r.strike === strike);
    const s = kind === "CE" ? row?.ce : row?.pe;
    if (!state || !s) return;
    setDraft((all) => [
      ...all,
      {
        key: key(),
        side,
        kind,
        qty: legSize,
        symbol: s.symbol,
        strike,
        expiry: state.expiry,
        expiry_token: state.expiry_token,
        bid: s.bid,
        ask: s.ask,
        mark: s.mark,
        stop: null,
        target: null,
        error: null,
      },
    ]);
  };

  const nudge = (k: string, by: 1 | -1) => {
    if (!state) return;
    setDraft((all) =>
      all.map((d) => {
        if (d.key !== k || d.strike === null || d.expiry !== state.expiry) return d;
        const strikes = state.rows.filter((r) => (d.kind === "CE" ? r.ce : r.pe)).map((r) => r.strike);
        const i = strikes.indexOf(d.strike);
        const next = strikes[i + by];
        if (i < 0 || next === undefined) return d;
        const row = state.rows.find((r) => r.strike === next);
        const s = d.kind === "CE" ? row?.ce : row?.pe;
        return s ? { ...d, strike: next, symbol: s.symbol, bid: s.bid, ask: s.ask, mark: s.mark } : d;
      }),
    );
  };

  const trade = () => (mode === "live" ? setAsk({ kind: "basket" }) : send(false));

  const send = (confirm: boolean) => {
    setAsk(null);
    setTrading(true);
    setError(null);
    void placeLive({
      mode,
      confirm,
      session_id: sessionId,
      underlying,
      legs: tradable.map((d) => ({
        symbol: d.symbol as string,
        side: d.side,
        qty: d.qty,
        stop: d.stop,
        target: d.target,
      })),
      rule,
    })
      .then((r) => {
        if (r.problem) setError(r.problem);
        setSessionId(r.session.id);
        want.current = { ...want.current, sessionId: r.session.id };
        setSessions((all) => [r.session, ...all.filter((s) => s.id !== r.session.id)]);
        setDraft([]);
        setRule(null);
        return refresh();
      })
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setTrading(false));
  };

  const live = now - lastOk < STALE_MS;
  const unit = state ? unitFor(state.currency, state.underlying, state.step) : null;
  const showDraft = tradable.length > 0 && preview?.payoff;
  const moment = state
    ? asMoment(
        state,
        showDraft ? (preview?.payoff ?? null) : state.payoff,
        showDraft ? (preview?.margin ?? 0) : state.margin,
      )
    : null;
  const mine = sessions.filter((s) => s.underlying === underlying && s.mode === mode);
  const canLive = markets.find((m) => m.underlying === underlying)?.live ?? false;
  const isLive = state?.session?.mode === "live";
  const groups = ["NSE", "Crypto"].map((g) => [g, markets.filter((m) => m.group === g)] as const);
  const sameExpiry = draft.every((d) => !d.expiry || d.expiry === state?.expiry);

  return (
    <main className="sim lb">
      <header className="sim-top">
        <BackButton onClick={onHome} />
        <h1>Live strategy builder</h1>
        <select
          value={underlying}
          onChange={(e) => {
            if (!markets.find((m) => m.underlying === e.target.value)?.live) setMode("paper");
            pick(e.target.value);
          }}
          aria-label="Underlying"
        >
          {markets.length === 0 && <option>{underlying}</option>}
          {groups.map(
            ([g, ms]) =>
              ms.length > 0 && (
                <optgroup key={g} label={g}>
                  {ms.map((m) => (
                    <option key={m.underlying}>{m.underlying}</option>
                  ))}
                </optgroup>
              ),
          )}
        </select>
        <span className={`sim-live${live && state?.open ? " on" : ""}`}>
          {!live ? "Not live" : state && !state.open ? "Closed" : "Live"}
        </span>
        {state && (
          <span className="sim-asof">
            {new Date(state.at).toLocaleTimeString("en-IN", { hour12: false, timeZone: "Asia/Kolkata" })} IST
          </span>
        )}
      </header>

      <div className="lb-templates">
        {templates.map((t) => (
          <button key={t.id} onClick={() => load(t.spec)} title={t.say} disabled={!state}>
            {t.name}
          </button>
        ))}
        {saved.length > 0 && (
          <select
            value=""
            onChange={(e) => {
              const s = saved.find((x) => x.id === Number(e.target.value));
              if (s) load(s.spec);
            }}
            aria-label="Load a saved strategy"
            disabled={!state}
          >
            <option value="">Saved strategy…</option>
            {saved.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name} · {s.underlying}
              </option>
            ))}
          </select>
        )}
      </div>

      {error && <p className="sim-error">{error}</p>}
      {!moment && !error && <p className="sim-empty">Connecting to the live chain…</p>}

      {moment && state && unit && (
        <div className="sim-body">
          <SimChain
            moment={moment}
            legs={moment.legs}
            onTrade={addFromChain}
            onExpiry={(day) => {
              const token = state.expiries.find((e) => e.expiry === day)?.token ?? "";
              setExpiry(token);
              want.current = { ...want.current, expiry: token };
              void refresh();
            }}
            loading={null}
          />
          <div className="sim-right">
            <SimPayoff
              moment={moment}
              dp={unit.dp}
              marginNote={
                showDraft
                  ? `What the draft would tie up, ${num(preview?.margin ?? 0, 0)} ${state.currency}. An estimate.`
                  : `What the open legs tie up, ${num(state.margin, 0)} ${state.currency}. An estimate by ${state.currency === "INR" ? "the NSE's SPAN and exposure" : "Shark's factors"}.`
              }
            />
            <DraftPanel
              legs={draft}
              rule={rule}
              preview={preview}
              unit={unit}
              size={legSize}
              step={step}
              onSize={setSize}
              onChange={(k, patch) =>
                setDraft((all) => all.map((d) => (d.key === k ? { ...d, ...patch } : d)))
              }
              onNudge={sameExpiry ? nudge : null}
              onRemove={(k) => setDraft((all) => all.filter((d) => d.key !== k))}
              onClear={() => {
                setDraft([]);
                setRule(null);
              }}
              onTrade={trade}
              trading={trading}
              blocked={state.open ? null : "The market is closed."}
              mode={mode}
              canLive={canLive}
              onMode={(m) => {
                setMode(m);
                setSessionId(null);
                want.current = { ...want.current, sessionId: null };
              }}
            />
            {state.account && <AccountStrip account={state.account} currency={state.currency} />}
            <LivePositions
              legs={state.legs}
              session={state.session}
              sessions={mine}
              unit={unit}
              onSession={(id) => {
                setSessionId(id);
                want.current = { ...want.current, sessionId: id };
                void refresh();
              }}
              onExit={(id) => (isLive ? setAsk({ kind: "exit", id }) : act(exitLiveLeg(id)))}
              onRemove={(id) => act(removeLiveLeg(id))}
              onLevels={(id, patch) => act(patchLiveLeg(id, patch))}
              onRule={(patch) => {
                if (sessionId === null) return;
                act(
                  patchLiveSession(sessionId, patch).then((s) =>
                    setSessions((all) => all.map((x) => (x.id === s.id ? s : x))),
                  ),
                );
              }}
              onExitAll={() => {
                if (sessionId === null) return;
                if (isLive) {
                  setAsk({ kind: "exitAll" });
                  return;
                }
                act(
                  exitAllLive(sessionId).then((r) => {
                    if (r.problems.length) setError(r.problems.join(" · "));
                  }),
                );
              }}
            />
          </div>
        </div>
      )}

      {ask && state && unit && (
        <ConfirmDialog
          title={
            ask.kind === "basket"
              ? `Send ${tradable.length} real order${tradable.length === 1 ? "" : "s"} to Shark?`
              : ask.kind === "exit"
                ? "Close this leg with a real order?"
                : "Close every live leg with real orders?"
          }
          go={ask.kind === "basket" ? "Place live order" : "Close for real"}
          onCancel={() => setAsk(null)}
          onConfirm={() => {
            if (ask.kind === "basket") {
              send(true);
              return;
            }
            setAsk(null);
            if (ask.kind === "exit") {
              act(exitLiveLeg(ask.id, true));
            } else if (sessionId !== null) {
              act(
                exitAllLive(sessionId, true).then((r) => {
                  if (r.problems.length) setError(r.problems.join(" · "));
                }),
              );
            }
          }}
        >
          {ask.kind === "basket" ? (
            <div className="lb-ask">
              <ul>
                {tradable.map((d) => (
                  <li key={d.key}>
                    <b className={d.side === "buy" ? "up" : "dn"}>{d.side === "buy" ? "Buy" : "Sell"}</b>{" "}
                    {unit.show(d.qty)} {state.underlying} {d.strike} {d.kind} · {d.expiry} · market, near{" "}
                    {num((d.side === "buy" ? d.ask : d.bid) ?? 0)}
                  </li>
                ))}
              </ul>
              {preview && (
                <p>
                  {preview.premium >= 0 ? "Credit" : "Debit"} about {num(Math.abs(preview.premium), 2)} · fees
                  about {num(preview.fees, 2)} · margin about {num(preview.margin, 0)} {state.currency}
                </p>
              )}
              {state.account?.available != null && (
                <p>
                  Options wallet: {num(state.account.available, 2)} {state.currency} free.
                </p>
              )}
              <p className="dim">
                Market orders, filled by Shark at whatever the book gives. Stops and targets are then watched
                by this app, not by Shark, and only while it is running.
              </p>
            </div>
          ) : (
            <p className="lb-ask">This sends a real market order on Shark.</p>
          )}
        </ConfirmDialog>
      )}
    </main>
  );
}

/** What Shark says the live account holds, and where it disagrees with the session. */
function AccountStrip({ account, currency }: { account: LiveAccount; currency: string }) {
  return (
    <section className="lb-account" aria-label="Shark account">
      <span className="lb-livebadge">LIVE</span>
      {account.error ? (
        <span className="dn">Shark account: {account.error}</span>
      ) : (
        <>
          <span>
            Options wallet <b>{account.available != null ? num(account.available, 2) : "—"}</b> {currency}{" "}
            free
          </span>
          <span className="dim">
            {account.positions.length} position{account.positions.length === 1 ? "" : "s"} on Shark
          </span>
          {account.mismatches.map((m) => (
            <span key={m} className="dn" title="Shark's positions and this session's open legs disagree">
              ⚠ {m}
            </span>
          ))}
        </>
      )}
    </section>
  );
}
