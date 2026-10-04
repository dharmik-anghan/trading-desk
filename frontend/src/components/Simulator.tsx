import { useCallback, useEffect, useRef, useState } from "react";
import { deleteSimSession, getOptbtUnderlyings, getSimMoment, getSimSessions, saveSimSession } from "../api";
import type { SimMoment, SimSession } from "../api";
import { BackButton } from "./BackButton";
import { SimChain } from "./sim/SimChain";
import { SimPayoff } from "./sim/SimPayoff";
import { SimPositions } from "./sim/SimPositions";
import type { Leg } from "./sim/legs";
import { merge, newId, strikeStep, toIn } from "./sim/legs";

interface Props {
  onHome: () => void;
}

const BACK = [
  ["-1d", "-1d"],
  ["sod", "SOD"],
  ["-1h", "-1h"],
  ["-15m", "-15m"],
  ["-5m", "-5m"],
  ["-1m", "-1m"],
] as const;
const FORWARD = [
  ["+1m", "+1m"],
  ["+5m", "+5m"],
  ["+15m", "+15m"],
  ["+1h", "+1h"],
  ["eod", "EOD"],
  ["+1d", "+1d"],
] as const;

const SPEEDS = [
  { label: "1 min / 1 s", move: "+1m", ms: 1000 },
  { label: "1 min / 0.5 s", move: "+1m", ms: 500 },
  { label: "5 min / 1 s", move: "+5m", ms: 1000 },
  { label: "15 min / 1 s", move: "+15m", ms: 1000 },
];

const sleep = (ms: number) => new Promise((r) => window.setTimeout(r, ms));

const longDate = (iso: string) =>
  new Date(iso).toLocaleString("en-IN", {
    weekday: "short",
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });

interface Ctx {
  underlying: string;
  at: string | null;
  expiry: string | null;
  multiplier: number;
}

/**
 * Any minute of the stored option history, traded by hand.
 *
 * The page holds the legs; every step sends them and gets them back filled,
 * stopped or settled as of the new moment. Steps go one at a time, in order,
 * so autoplay and a click in the middle of it cannot cross.
 */
export function Simulator({ onHome }: Props) {
  const [underlyings, setUnderlyings] = useState<string[]>(["NIFTY"]);
  const [moment, setMoment] = useState<SimMoment | null>(null);
  const [legs, setLegs] = useState<Leg[]>([]);
  const [multiplier, setMultiplier] = useState(1);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(0);
  const [session, setSession] = useState<{ id: number; name: string } | null>(null);
  const [saving, setSaving] = useState<string | null>(null);
  const [saved, setSaved] = useState<SimSession[] | null>(null);

  const ctx = useRef<Ctx>({
    underlying: "NIFTY",
    at: null,
    expiry: null,
    multiplier: 1,
  });
  const legsRef = useRef<Leg[]>([]);
  const queue = useRef<Promise<unknown>>(Promise.resolve());
  const playRef = useRef(false);

  const putLegs = useCallback((next: Leg[]) => {
    legsRef.current = next;
    setLegs(next);
  }, []);

  /** One step to the server, after any already on their way. */
  const sync = useCallback(
    (opts: { move?: string; at?: string | null; expiry?: string | null } = {}) => {
      const run = async () => {
        const c = ctx.current;
        setBusy(true);
        try {
          const m = await getSimMoment({
            underlying: c.underlying,
            at: opts.at !== undefined ? opts.at : c.at,
            move: opts.move ?? null,
            expiry: opts.expiry !== undefined ? opts.expiry : c.expiry,
            since: c.at,
            multiplier: c.multiplier,
            legs: legsRef.current.map(toIn),
          });
          ctx.current = { ...ctx.current, at: m.at, expiry: m.expiry };
          setMoment(m);
          putLegs(merge(legsRef.current, m.legs));
          setError(null);
          return m;
        } catch (e) {
          setError(e instanceof Error ? e.message : String(e));
          playRef.current = false;
          setPlaying(false);
          return null;
        } finally {
          setBusy(false);
        }
      };
      const next = queue.current.then(run);
      queue.current = next;
      return next;
    },
    [putLegs],
  );

  useEffect(() => {
    void getOptbtUnderlyings()
      .then((list) => list.length && setUnderlyings(list.map((u) => u.underlying)))
      .catch(() => undefined);
    void sync();
  }, [sync]);

  // Autoplay: one step, wait, the next - never two in flight.
  useEffect(() => {
    playRef.current = playing;
    if (!playing) return;
    let alive = true;
    void (async () => {
      while (alive && playRef.current) {
        const m = await sync({ move: SPEEDS[speed].move });
        if (!m || m.at === m.last) {
          playRef.current = false;
          setPlaying(false);
          return;
        }
        await sleep(SPEEDS[speed].ms);
      }
    })();
    return () => {
      alive = false;
    };
  }, [playing, speed, sync]);

  const edit = (next: Leg[]) => {
    putLegs(next);
    void sync();
  };
  const at = moment?.at ?? null;
  const fresh = (leg: Leg): Leg => ({
    ...leg,
    entry_at: at ?? leg.entry_at,
    entry_price: null,
    stop: null,
    target: null,
    exit_at: null,
    exit_price: null,
    exit_reason: null,
    status: undefined,
    ltp: undefined,
    error: undefined,
  });

  const trade = (side: "buy" | "sell", kind: "CE" | "PE", strike: number) => {
    if (!moment?.expiry || !at) return;
    edit([
      ...legsRef.current,
      fresh({
        id: newId(),
        side,
        kind,
        strike,
        expiry: moment.expiry,
        lots: 1,
        entry_at: at,
        entry_price: null,
        stop: null,
        target: null,
        exit_at: null,
        exit_price: null,
        exit_reason: null,
        enabled: true,
      }),
    ]);
  };
  const patch = (id: string, f: (l: Leg) => Leg) =>
    edit(legsRef.current.map((l) => (l.id === id ? f(l) : l)));

  const reset = () => {
    setPlaying(false);
    setSession(null);
    putLegs([]);
    void sync();
  };

  const openSaved = () => {
    if (saved) {
      setSaved(null);
      return;
    }
    void getSimSessions()
      .then(setSaved)
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
  };

  const load = (s: SimSession) => {
    setPlaying(false);
    setSaved(null);
    setSession({ id: s.id, name: s.name });
    setMultiplier(s.state.multiplier);
    ctx.current = {
      underlying: s.underlying,
      at: null,
      expiry: s.state.expiry,
      multiplier: s.state.multiplier,
    };
    putLegs(s.state.legs);
    void sync({ at: s.at });
  };

  const save = (name: string, asNew: boolean) => {
    if (!at) return;
    void saveSimSession({
      id: asNew ? null : (session?.id ?? null),
      name,
      underlying: ctx.current.underlying,
      at,
      state: {
        legs: legsRef.current.map(toIn),
        expiry: ctx.current.expiry,
        multiplier,
      },
    })
      .then((s) => {
        setSession({ id: s.id, name: s.name });
        setSaving(null);
      })
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
  };

  const step = moment ? strikeStep(moment.rows, moment.atm) : 50;

  return (
    <main className="sim">
      <header className="sim-top">
        <BackButton onClick={onHome} />
        <h1>Simulator</h1>
        <select
          value={ctx.current.underlying}
          onChange={(e) => {
            setPlaying(false);
            setSession(null);
            ctx.current = {
              ...ctx.current,
              underlying: e.target.value,
              at: null,
              expiry: null,
            };
            putLegs([]);
            void sync({ at: null });
          }}
          aria-label="Underlying"
        >
          {underlyings.map((u) => (
            <option key={u}>{u}</option>
          ))}
        </select>
        <div className="sim-files">
          {saving !== null ? (
            <form
              className="sim-save"
              onSubmit={(e) => {
                e.preventDefault();
                if (saving.trim()) save(saving.trim(), true);
              }}
            >
              <input
                autoFocus
                value={saving}
                placeholder="Name"
                onChange={(e) => setSaving(e.target.value)}
                aria-label="Session name"
              />
              <button type="submit" disabled={!saving.trim()}>
                Save
              </button>
              <button type="button" onClick={() => setSaving(null)}>
                Cancel
              </button>
            </form>
          ) : (
            <>
              <button
                className="sim-primary"
                onClick={() => (session ? save(session.name, false) : setSaving(""))}
                title={session ? `Overwrite “${session.name}”` : undefined}
                disabled={!at}
              >
                Save
              </button>
              {session && <button onClick={() => setSaving(`${session.name} copy`)}>Save as</button>}
            </>
          )}
          <div className="sim-saved">
            <button onClick={openSaved} aria-expanded={saved !== null}>
              Saved
            </button>
            {saved && (
              <ul>
                {saved.length === 0 && <li className="dim">Nothing saved yet</li>}
                {saved.map((s) => (
                  <li key={s.id}>
                    <button onClick={() => load(s)}>
                      <b>{s.name}</b>
                      <span>
                        {s.underlying} · {longDate(s.at)} · {s.state.legs.length} legs
                      </span>
                    </button>
                    <button
                      className="x"
                      aria-label={`Delete ${s.name}`}
                      onClick={() =>
                        void deleteSimSession(s.id).then(() => {
                          setSaved((all) => all?.filter((x) => x.id !== s.id) ?? null);
                          if (session?.id === s.id) setSession(null);
                        })
                      }
                    >
                      ✕
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
          <button className="sim-reset" onClick={reset}>
            Reset
          </button>
          {session && <span className="sim-name">{session.name}</span>}
        </div>
        <div className="sim-play">
          <button className={playing ? "on" : ""} onClick={() => setPlaying((p) => !p)} disabled={!moment}>
            {playing ? "❚❚ Pause" : "▶ Autoplay"}
          </button>
          <select
            value={speed}
            onChange={(e) => setSpeed(Number(e.target.value))}
            aria-label="Autoplay speed"
          >
            {SPEEDS.map((s, i) => (
              <option key={s.label} value={i}>
                {s.label}
              </option>
            ))}
          </select>
        </div>
      </header>

      <nav className="sim-time" aria-label="Move in time">
        {BACK.map(([move, label]) => (
          <button
            key={move}
            onClick={() => void sync({ move })}
            disabled={!moment || (moment.at === moment.first && move !== "sod")}
          >
            {label}
          </button>
        ))}
        <label className="sim-when">
          <span>{moment ? longDate(moment.at) : "…"}</span>
          <input
            type="datetime-local"
            value={moment?.at.slice(0, 16) ?? ""}
            min={moment?.first.slice(0, 16)}
            max={moment?.last.slice(0, 16)}
            onChange={(e) => e.target.value && void sync({ at: e.target.value })}
            aria-label="Go to a moment"
          />
        </label>
        {FORWARD.map(([move, label]) => (
          <button
            key={move}
            onClick={() => void sync({ move })}
            disabled={!moment || (moment.at === moment.last && move !== "eod")}
          >
            {label}
          </button>
        ))}
      </nav>

      {error && <p className="sim-error">{error}</p>}

      {moment && (
        <div className={`sim-body${busy && !playing ? " busy" : ""}`}>
          <SimChain
            moment={moment}
            legs={legs}
            onTrade={trade}
            onExpiry={(expiry) => {
              ctx.current = { ...ctx.current, expiry };
              void sync({ expiry });
            }}
          />
          <div className="sim-right">
            <SimPayoff moment={moment} />
            <SimPositions
              moment={moment}
              legs={legs}
              multiplier={multiplier}
              step={step}
              onMultiplier={(n) => {
                setMultiplier(n);
                ctx.current = { ...ctx.current, multiplier: n };
                void sync();
              }}
              onChange={(id, p) => patch(id, (l) => ({ ...l, ...p }))}
              onRetrade={(id, p) => patch(id, (l) => fresh({ ...l, ...p }))}
              onExit={(id) =>
                patch(id, (l) => ({
                  ...l,
                  exit_at: at,
                  exit_price: null,
                  exit_reason: "exit",
                }))
              }
              onReenter={(id) => {
                const l = legsRef.current.find((x) => x.id === id);
                if (l) edit([...legsRef.current, fresh({ ...l, id: newId() })]);
              }}
              onRemove={(id) => edit(legsRef.current.filter((l) => l.id !== id))}
              onToggleAll={(on) => edit(legsRef.current.map((l) => ({ ...l, enabled: on })))}
            />
          </div>
        </div>
      )}
    </main>
  );
}
