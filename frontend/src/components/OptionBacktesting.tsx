import { useEffect, useRef, useState } from "react";
import {
  deleteStrategy,
  getCryptoUnderlyings,
  getOptbtUnderlyings,
  getStrategies,
  getStrategyTemplates,
  runOptbt,
  saveStrategy,
} from "../api";
import type {
  CryptoUnderlying,
  SavedStrategy,
  OptionStrategySpec,
  StrategyTemplate,
  OptbtAdjust,
  OptbtCoverage,
  OptbtDays,
  OptbtEntrySignal,
  OptbtExitSignal,
  OptbtExpiryChoice,
  OptbtReEntry,
  OptbtResult,
  OptbtTrigger,
} from "../api";
import { BackButton } from "./BackButton";
import { LegRow } from "./optbt/LegRow";
import { OptResult } from "./optbt/OptResult";
import { OPENED } from "./optbt/explore";
import { ExpiryPicker } from "./optbt/ExpiryPicker";
import { SignalRows } from "./optbt/SignalRows";
import { NEAREST_WEEKLY, copyLeg, defaultLegs, fromRequest, leg, toRequest } from "./optbt/legs";
import type { LegDraft } from "./optbt/legs";

interface Props {
  onHome: () => void;
}

const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
/** The NSE trades five days; a crypto market all seven. */
const allDays = (crypto: boolean) => (crypto ? [0, 1, 2, 3, 4, 5, 6] : [0, 1, 2, 3, 4]);
/** "09:20:00" as the API writes a time, "09:20" as a time input wants it. */
const hhmm = (t: string) => t.slice(0, 5);

const ANY_DAY: OptbtDays = {
  expiry_day: "any",
  dte_min: null,
  dte_max: null,
  vix_min: null,
  vix_max: null,
  vix_pct_min: null,
  vix_pct_max: null,
  vix_lookback: 252,
  gap_min: null,
  gap_max: null,
  open_zones: [],
};

/** How many entry conditions are set - shown on the folded section. */
function countDays(d: OptbtDays): number {
  return [
    d.expiry_day !== "any",
    d.dte_min !== null || d.dte_max !== null,
    d.vix_min !== null || d.vix_max !== null,
    d.vix_pct_min !== null || d.vix_pct_max !== null,
    d.gap_min !== null || d.gap_max !== null,
    d.open_zones.length > 0,
  ].filter(Boolean).length;
}

/**
 * An option strategy, built leg by leg: saved, run over stored history on the
 * NSE, or - for a crypto underlying, which has no stored history - kept to paper
 * trade on the live chain.
 *
 * The strategy is what runs; the results below can then be filtered without
 * running it again. "Trade only when" is part of the strategy - conditions
 * checked before each entry - which is what a positional trade or the
 * optimiser needs, and is folded away until wanted.
 */
export function OptionBacktesting({ onHome }: Props) {
  const [underlyings, setUnderlyings] = useState<OptbtCoverage[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [underlying, setUnderlying] = useState("NIFTY");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");

  const [crypto, setCrypto] = useState<CryptoUnderlying[]>([]);
  const [templates, setTemplates] = useState<StrategyTemplate[]>([]);
  const [saved, setSaved] = useState<SavedStrategy[]>([]);
  /** The saved strategy on screen, which Save overwrites; null saves a new one. */
  const [savedId, setSavedId] = useState<number | null>(null);
  const [name, setName] = useState("");
  const [saveError, setSaveError] = useState<string | null>(null);

  const [legs, setLegs] = useState<LegDraft[]>(defaultLegs);
  const [hold, setHold] = useState<"intraday" | "expiry">("intraday");
  const [expiry, setExpiry] = useState<OptbtExpiryChoice>(NEAREST_WEEKLY);
  const [entry, setEntry] = useState("09:20");
  const [exit, setExit] = useState("15:15");
  const [weekdays, setWeekdays] = useState<number[]>([0, 1, 2, 3, 4]);
  const [days, setDays] = useState<OptbtDays>(ANY_DAY);
  const [stop, setStop] = useState<{ value: number | null; unit: "rs" | "credit" }>({
    value: null,
    unit: "rs",
  });
  const [target, setTarget] = useState<{ value: number | null; unit: "rs" | "credit" }>({
    value: null,
    unit: "rs",
  });
  const [exitDte, setExitDte] = useState<number | null>(null);
  const [adjust, setAdjust] = useState<OptbtAdjust>({
    enabled: false,
    near_points: 50,
    fall_from: "long",
    fall_points: 200,
    rise_from: "short",
    rise_points: 0,
    move_wing: true,
    max_per_trade: 1,
  });
  const [equalWings, setEqualWings] = useState(false);
  const [trigger, setTrigger] = useState<OptbtTrigger>({
    mode: "time",
    move_pct: 0.5,
    range_until: null,
  });
  const [reentry, setReentry] = useState<OptbtReEntry>({
    enabled: false,
    trigger: "leg_stop",
    max_times: 1,
  });
  const [entrySignal, setEntrySignal] = useState<OptbtEntrySignal>({
    mode: "take_if",
    join: "all",
    conditions: [],
  });
  const [exitSignal, setExitSignal] = useState<OptbtExitSignal>({ join: "any", conditions: [] });
  const [trail, setTrail] = useState(false);
  const [slippage, setSlippage] = useState(0.3);
  const [minSlip, setMinSlip] = useState(0.05);
  const [brokerage, setBrokerage] = useState(20);

  const [result, setResult] = useState<OptbtResult | null>(null);
  const [finished, setFinished] = useState<{ at: Date; seconds: number } | null>(null);
  const [running, setRunning] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const started = useRef(0);

  useEffect(() => {
    void getOptbtUnderlyings()
      .then((list) => {
        setUnderlyings(list);
        const first = list.find((u) => u.underlying === "NIFTY") ?? list[0];
        if (first) {
          setUnderlying(first.underlying);
          setStart(first.first_day ?? "");
          setEnd(first.last_day ?? "");
        }
      })
      .catch((e: unknown) => setLoadError(e instanceof Error ? e.message : String(e)));
    // Neither is needed to build or backtest, so a failure leaves them empty.
    void getCryptoUnderlyings()
      .then(setCrypto)
      .catch(() => setCrypto([]));
    void getStrategyTemplates()
      .then(setTemplates)
      .catch(() => setTemplates([]));
    void getStrategies()
      .then(setSaved)
      .catch(() => setSaved([]));
  }, []);

  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(
      () => setElapsed(Math.round((performance.now() - started.current) / 1000)),
      500,
    );
    return () => window.clearInterval(timer);
  }, [running]);

  const window_ = underlyings?.find((u) => u.underlying === underlying);
  const coin = crypto.find((c) => c.underlying === underlying) ?? null;
  const isCrypto = coin !== null;
  const currency = isCrypto ? "$" : "₹";
  const timesOk = hold === "expiry" || entry < exit;
  const canRun =
    !isCrypto &&
    Boolean(start && end && start <= end) && timesOk && legs.length > 0 && weekdays.length > 0 && !running;
  const pick = (next: string) => {
    const goingCrypto = crypto.some((c) => c.underlying === next);
    if (goingCrypto !== isCrypto) {
      setWeekdays(allDays(goingCrypto));
      if (!goingCrypto && expiry.series === "daily") setExpiry({ ...expiry, series: "weekly" });
    }
    setUnderlying(next);
    const u = underlyings?.find((x) => x.underlying === next);
    if (u) {
      setStart(u.first_day ?? "");
      setEnd(u.last_day ?? "");
    }
  };

  /** The strategy on screen, as a spec: what Save keeps and a run is sent. */
  const spec = (): OptionStrategySpec => ({
    legs: legs.map((l) => toRequest(l, expiry)),
    expiry,
    entry,
    exit,
    weekdays,
    hold,
    mtm_stop: stop.unit === "rs" ? stop.value : null,
    mtm_target: target.unit === "rs" ? target.value : null,
    stop_credit: stop.unit === "credit" && stop.value !== null ? stop.value / 100 : null,
    target_credit: target.unit === "credit" && target.value !== null ? target.value / 100 : null,
    exit_dte: hold === "expiry" ? exitDte : null,
    trail_to_cost: trail,
    days,
    adjust: { ...adjust, enabled: adjust.enabled && hold === "expiry" },
    equal_wings: equalWings,
    trigger,
    reentry: { ...reentry, enabled: reentry.enabled && hold === "intraday" },
    entry_signal: entrySignal,
    exit_signal: exitSignal,
    slippage: slippage / 100,
    min_slip: minSlip,
    brokerage,
  });

  /** A spec into the form: a template, or a saved strategy. */
  const load = (s: OptionStrategySpec) => {
    setLegs(s.legs.map(fromRequest));
    setExpiry(s.expiry);
    setEntry(hhmm(s.entry));
    setExit(hhmm(s.exit));
    setWeekdays(s.weekdays);
    setHold(s.hold);
    const level = (rs: number | null, credit: number | null) =>
      credit !== null
        ? { value: Math.round(credit * 1000) / 10, unit: "credit" as const }
        : { value: rs, unit: "rs" as const };
    setStop(level(s.mtm_stop, s.stop_credit));
    setTarget(level(s.mtm_target, s.target_credit));
    setExitDte(s.exit_dte);
    setTrail(s.trail_to_cost);
    setDays(s.days);
    setAdjust(s.adjust);
    setEqualWings(s.equal_wings);
    setTrigger({
      ...s.trigger,
      range_until: s.trigger.range_until ? hhmm(s.trigger.range_until) : null,
    });
    setReentry(s.reentry);
    setEntrySignal(s.entry_signal);
    setExitSignal(s.exit_signal);
    setSlippage(Math.round(s.slippage * 10000) / 100);
    setMinSlip(s.min_slip);
    setBrokerage(s.brokerage);
  };

  const loadTemplate = (t: StrategyTemplate) => {
    load(t.spec);
    // A template is written for any market; the days it trades are this one's.
    setWeekdays(allDays(isCrypto));
    setSavedId(null);
    setName("");
  };

  const loadSaved = (id: number) => {
    const s = saved.find((x) => x.id === id);
    if (!s) return;
    load(s.spec);
    pick(s.underlying);
    setWeekdays(s.spec.weekdays);
    setSavedId(s.id);
    setName(s.name);
  };

  const save = (asNew: boolean) => {
    setSaveError(null);
    void saveStrategy({
      id: asNew || savedId === null ? undefined : savedId,
      name: name.trim(),
      underlying,
      spec: spec(),
    })
      .then((s) => {
        setSavedId(s.id);
        setSaved((all) => [s, ...all.filter((x) => x.id !== s.id)]);
      })
      .catch((e: unknown) => setSaveError(e instanceof Error ? e.message : String(e)));
  };

  const remove = () => {
    if (savedId === null) return;
    const id = savedId;
    void deleteStrategy(id)
      .then(() => {
        setSaved((all) => all.filter((x) => x.id !== id));
        setSavedId(null);
      })
      .catch((e: unknown) => setSaveError(e instanceof Error ? e.message : String(e)));
  };

  const run = () => {
    setRunning(true);
    setElapsed(0);
    setError(null);
    started.current = performance.now();
    void runOptbt({ ...spec(), underlying, start, end })
      .then((r) => {
        setResult(r);
        setFinished({ at: new Date(), seconds: (performance.now() - started.current) / 1000 });
      })
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setRunning(false));
  };

  const update = (i: number, next: LegDraft) => setLegs((all) => all.map((l, k) => (k === i ? next : l)));
  const conditions = countDays(days);
  const exits =
    [stop.value, target.value, hold === "expiry" ? exitDte : null].filter((v) => v !== null).length +
    (trail ? 1 : 0) +
    (hold === "intraday" && reentry.enabled ? 1 : 0);

  return (
    <main className="bt obt ob">
      <header className="ob-top">
        <BackButton onClick={onHome} />
        <h1>Strategy builder</h1>
        <div className="ob-scope">
          <select
            value={underlying}
            onChange={(e) => pick(e.target.value)}
            aria-label="Underlying"
            disabled={!underlyings?.length && !crypto.length}
          >
            <optgroup label="NSE">
              {(underlyings ?? [{ underlying: "NIFTY" } as OptbtCoverage]).map((u) => (
                <option key={u.underlying} value={u.underlying}>
                  {u.underlying}
                </option>
              ))}
            </optgroup>
            {crypto.length > 0 && (
              <optgroup label="Crypto">
                {crypto.map((c) => (
                  <option key={c.underlying} value={c.underlying}>
                    {c.underlying}
                  </option>
                ))}
              </optgroup>
            )}
          </select>
          {!isCrypto && (
            <>
              <input
                type="date"
                value={start}
                min={window_?.first_day ?? undefined}
                max={window_?.last_day ?? undefined}
                onChange={(e) => setStart(e.target.value)}
                aria-label="From"
              />
              <span className="ob-to">to</span>
              <input
                type="date"
                value={end}
                min={window_?.first_day ?? undefined}
                max={window_?.last_day ?? undefined}
                onChange={(e) => setEnd(e.target.value)}
                aria-label="To"
              />
            </>
          )}
        </div>
      </header>

      {loadError && <p className="ob-error">{loadError}</p>}
      {!isCrypto && underlyings && !underlyings.length && (
        <p className="ob-error">No option history yet. Run scripts/backfill_options.py to fetch it.</p>
      )}

      <section className="ob-strategy" aria-label="Strategy">
        <div className="ob-presets">
          {templates.map((t) => (
            <button key={t.id} onClick={() => loadTemplate(t)} title={t.say}>
              {t.name}
            </button>
          ))}
          <div className="ob-saved">
            <select
              value={savedId ?? ""}
              onChange={(e) => e.target.value && loadSaved(Number(e.target.value))}
              aria-label="Saved strategies"
              disabled={!saved.length}
            >
              <option value="">{saved.length ? "Saved…" : "None saved"}</option>
              {saved.map((x) => (
                <option key={x.id} value={x.id}>
                  {x.name} · {x.underlying}
                </option>
              ))}
            </select>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Name"
              aria-label="Strategy name"
              maxLength={80}
            />
            <button onClick={() => save(false)} disabled={!name.trim()}>
              Save
            </button>
            {savedId !== null && (
              <>
                <button
                  onClick={() => save(true)}
                  disabled={!name.trim()}
                  title="Keep the saved one, save this beside it"
                >
                  Save as new
                </button>
                <button onClick={remove} title="Delete the saved strategy" aria-label="Delete saved strategy">
                  ✕
                </button>
              </>
            )}
            {saveError && <span className="ob-error">{saveError}</span>}
          </div>
        </div>

        <div className="ob-legs">
          {legs.map((l, i) => (
            <LegRow
              key={l.id}
              index={i}
              leg={l}
              onChange={(next) => update(i, next)}
              onCopy={() => setLegs((all) => [...all.slice(0, i + 1), copyLeg(l), ...all.slice(i + 1)])}
              onRemove={legs.length > 1 ? () => setLegs((all) => all.filter((_, k) => k !== i)) : null}
              daysSeries={expiry.series === "days"}
              coin={coin && { name: coin.underlying, step: coin.qty_step }}
              currency={currency}
            />
          ))}
          <div className="ob-legfoot">
            <button className="ob-add" onClick={() => setLegs((l) => [...l, leg("sell", "CE")])}>
              + Add leg
            </button>
            <label
              className="ob-field ob-check"
              title="After strikes are picked, both wings of a condor are set to the same width - the average of the two"
            >
              <input type="checkbox" checked={equalWings} onChange={(e) => setEqualWings(e.target.checked)} />
              <span>Equal wings</span>
            </label>
          </div>
        </div>

        <div className="ob-timing">
          <ExpiryPicker value={expiry} onChange={setExpiry} daily={isCrypto} />
          <div className="ob-seg" aria-label="Holding">
            <button className={hold === "intraday" ? "on" : ""} onClick={() => setHold("intraday")}>
              Intraday
            </button>
            <button className={hold === "expiry" ? "on" : ""} onClick={() => setHold("expiry")}>
              Positional
            </button>
          </div>
          <label className="ob-field" title={trigger.mode !== "time" ? "Earliest the trigger starts watching" : undefined}>
            <span>Enter</span>
            <input type="time" step={60} value={entry} onChange={(e) => setEntry(e.target.value)} />
          </label>
          <div className="ob-field" title="Trade at the clock time, once spot has moved a percent from it, or once spot closes outside the range formed before a time">
            <span>Trigger</span>
            <div className="ob-limit">
              <select
                value={trigger.mode}
                onChange={(e) =>
                  setTrigger({ ...trigger, mode: e.target.value as OptbtTrigger["mode"] })
                }
                aria-label="Entry trigger"
              >
                <option value="time">At the time</option>
                <option value="move_pct">On a % move</option>
                <option value="range_breakout">On a range breakout</option>
              </select>
              {trigger.mode === "move_pct" && (
                <input
                  type="number"
                  min={0.1}
                  step={0.1}
                  value={trigger.move_pct}
                  onChange={(e) =>
                    setTrigger({ ...trigger, move_pct: Math.max(0.1, Number(e.target.value)) })
                  }
                  aria-label="Percent spot must move from its price at entry"
                />
              )}
              {trigger.mode === "range_breakout" && (
                <input
                  type="time"
                  step={60}
                  value={trigger.range_until ?? entry}
                  onChange={(e) => setTrigger({ ...trigger, range_until: e.target.value })}
                  aria-label="The range runs from entry to this time"
                />
              )}
            </div>
          </div>
          <label className="ob-field" title={hold === "expiry" ? "On the day the nearest leg expires" : undefined}>
            <span>{hold === "expiry" ? "Exit on expiry day" : "Exit"}</span>
            <input type="time" step={60} value={exit} onChange={(e) => setExit(e.target.value)} />
          </label>
          <div className="ob-seg days" aria-label="Weekdays">
            {WEEKDAYS.slice(0, isCrypto ? 7 : 5).map((d, i) => (
              <button
                key={d}
                className={weekdays.includes(i) ? "on" : ""}
                aria-pressed={weekdays.includes(i)}
                onClick={() =>
                  setWeekdays((w) => (w.includes(i) ? w.filter((x) => x !== i) : [...w, i].sort()))
                }
              >
                {d}
              </button>
            ))}
          </div>
        </div>

        <details className="ob-more">
          <summary>
            Trade only when{conditions > 0 && <em>{conditions}</em>}
          </summary>
          <div className="ob-grid">
            <label className="ob-field">
              <span>Expiry day</span>
              <select
                value={days.expiry_day}
                onChange={(e) => setDays({ ...days, expiry_day: e.target.value as OptbtDays["expiry_day"] })}
              >
                <option value="any">Any day</option>
                <option value="only">Only expiry day</option>
                <option value="skip">Never expiry day</option>
                <option value="skip_eve">Not expiry day or the day before</option>
              </select>
            </label>
            <Range
              label="Days to expiry"
              lo={days.dte_min}
              hi={days.dte_max}
              onChange={(lo, hi) => setDays({ ...days, dte_min: lo, dte_max: hi })}
            />
            {!isCrypto && (
              <>
                <Range
                  label="VIX"
                  lo={days.vix_min}
                  hi={days.vix_max}
                  onChange={(lo, hi) => setDays({ ...days, vix_min: lo, vix_max: hi })}
                />
                <Range
                  label="VIX percentile"
                  lo={days.vix_pct_min}
                  hi={days.vix_pct_max}
                  onChange={(lo, hi) => setDays({ ...days, vix_pct_min: lo, vix_pct_max: hi })}
                  title="0–100, ranked against the previous year of sessions"
                />
                <Range
                  label="Gap at open, %"
                  lo={days.gap_min}
                  hi={days.gap_max}
                  onChange={(lo, hi) => setDays({ ...days, gap_min: lo, gap_max: hi })}
                />
            <label className="ob-field" title="Classic pivots from the previous day's high, low and close">
                  <span>Opened</span>
                  <select
                    value={Object.keys(OPENED).find((k) => same(OPENED[k].zones, days.open_zones)) ?? ""}
                    onChange={(e) =>
                  setDays({ ...days, open_zones: e.target.value ? OPENED[e.target.value].zones : [] })
                    }
                  >
                    <option value="">Anywhere</option>
                    {Object.entries(OPENED).map(([k, o]) => (
                      <option key={k} value={k}>
                        {o.label}
                      </option>
                    ))}
                  </select>
                </label>
              </>
            )}
            {conditions > 0 && (
              <button className="ob-reset" onClick={() => setDays(ANY_DAY)}>
                Clear conditions
              </button>
            )}
          </div>
        </details>

        <details className="ob-more">
          <summary>
            Indicators
            {entrySignal.conditions.length + exitSignal.conditions.length > 0 && (
              <em>{entrySignal.conditions.length + exitSignal.conditions.length}</em>
            )}
          </summary>
          <div className="ob-sig">
            <div className="ob-sighead">
              <span>Entry</span>
              <select
                value={entrySignal.mode}
                onChange={(e) =>
                  setEntrySignal({ ...entrySignal, mode: e.target.value as OptbtEntrySignal["mode"] })
                }
                aria-label="What the entry conditions do"
                title="Take only if / skip if: judged once, at the entry. Wait until: enters on the first bar they hold, up to the exit time; also applies to re-entries."
              >
                <option value="take_if">Take only if</option>
                <option value="skip_if">Skip if</option>
                <option value="wait">Wait until</option>
              </select>
              {entrySignal.conditions.length > 1 && (
                <Join value={entrySignal.join} onChange={(join) => setEntrySignal({ ...entrySignal, join })} />
              )}
            </div>
            <SignalRows
              value={entrySignal.conditions}
              onChange={(conditions) => setEntrySignal({ ...entrySignal, conditions })}
            />
            <div className="ob-sighead">
              <span>Exit when</span>
              {exitSignal.conditions.length > 1 && (
                <Join value={exitSignal.join} onChange={(join) => setExitSignal({ ...exitSignal, join })} />
              )}
            </div>
            <SignalRows
              value={exitSignal.conditions}
              onChange={(conditions) => setExitSignal({ ...exitSignal, conditions })}
            />
          </div>
        </details>

        <details className="ob-more">
          <summary>
            Exit the whole position{exits > 0 && <em>{exits}</em>}
          </summary>
          <div className="ob-grid">
            <Limit label="Stop at a loss of" value={stop} onChange={setStop} currency={currency} />
            <Limit label="Take profit at" value={target} onChange={setTarget} currency={currency} />
            {hold === "expiry" && (
              <label className="ob-field" title="Closes at the exit time on that day, whatever the P&L">
                <span>Close at days to expiry</span>
                <input
                  type="number"
                  min={0}
                  placeholder="off"
                  value={exitDte ?? ""}
                  onChange={(e) =>
                    setExitDte(e.target.value === "" ? null : Math.max(0, Number(e.target.value)))
                  }
                />
              </label>
            )}
            <label className="ob-field">
              <span>After one leg stops</span>
              <select value={trail ? "cost" : "keep"} onChange={(e) => setTrail(e.target.value === "cost")}>
                <option value="keep">Leave the others</option>
                <option value="cost">Move their stops to cost</option>
              </select>
            </label>
            {hold === "intraday" && (
              <div className="ob-field" title="Sells the same legs again, fresh, at the price when it re-enters">
                <span>After the position goes flat</span>
                <div className="ob-limit">
                  <select
                    value={reentry.enabled ? reentry.trigger : "off"}
                    onChange={(e) =>
                      e.target.value === "off"
                        ? setReentry({ ...reentry, enabled: false })
                        : setReentry({
                            ...reentry,
                            enabled: true,
                            trigger: e.target.value as OptbtReEntry["trigger"],
                          })
                    }
                    aria-label="Re-enter after"
                  >
                    <option value="off">Stay out for the day</option>
                    <option value="leg_stop">Re-enter after a leg's own stop</option>
                    <option value="mtm_stop">Re-enter after the whole-position stop</option>
                    <option value="any">Re-enter after any exit</option>
                  </select>
                  {reentry.enabled && (
                    <input
                      type="number"
                      min={1}
                      max={10}
                      value={reentry.max_times}
                      onChange={(e) =>
                        setReentry({
                          ...reentry,
                          max_times: Math.max(1, Math.min(10, Math.round(Number(e.target.value)))),
                        })
                      }
                      aria-label="Re-entries at most, per day"
                      title="At most, per day"
                    />
                  )}
                </div>
              </div>
            )}
          </div>
        </details>

        {hold === "expiry" && (
          <details className="ob-more">
            <summary>
              Adjust{adjust.enabled && <em>on</em>}
            </summary>
            <div className="ob-grid">
              <label className="ob-field ob-check">
                <input
                  type="checkbox"
                  checked={adjust.enabled}
                  onChange={(e) => setAdjust({ ...adjust, enabled: e.target.checked })}
                />
                <span>Move the untested side in when spot nears a wing</span>
              </label>
              <Points
                label="Spot within, points of a long strike"
                value={adjust.near_points}
                onChange={(v) => setAdjust({ ...adjust, near_points: v })}
              />
              <div className="ob-field">
                <span>On a fall: new short call</span>
                <div className="ob-limit">
                  <input
                    type="number"
                    step={50}
                    value={adjust.fall_points}
                    onChange={(e) => setAdjust({ ...adjust, fall_points: Number(e.target.value) })}
                    aria-label="Points above"
                  />
                  <select
                    value={adjust.fall_from}
                    onChange={(e) => setAdjust({ ...adjust, fall_from: e.target.value as "long" | "short" })}
                    aria-label="Above which put"
                  >
                    <option value="long">pts above the long put</option>
                    <option value="short">pts above the short put</option>
                  </select>
                </div>
              </div>
              <div className="ob-field" title="0 below the short call is an iron fly">
                <span>On a rise: new short put</span>
                <div className="ob-limit">
                  <input
                    type="number"
                    step={50}
                    value={adjust.rise_points}
                    onChange={(e) => setAdjust({ ...adjust, rise_points: Number(e.target.value) })}
                    aria-label="Points below"
                  />
                  <select
                    value={adjust.rise_from}
                    onChange={(e) => setAdjust({ ...adjust, rise_from: e.target.value as "long" | "short" })}
                    aria-label="Below which call"
                  >
                    <option value="short">pts below the short call</option>
                    <option value="long">pts below the long call</option>
                  </select>
                </div>
              </div>
              <label className="ob-field ob-check">
                <input
                  type="checkbox"
                  checked={adjust.move_wing}
                  onChange={(e) => setAdjust({ ...adjust, move_wing: e.target.checked })}
                />
                <span>Move the wing too, same width</span>
              </label>
              <Count
                label="At most, per trade"
                value={adjust.max_per_trade}
                min={1}
                onChange={(v) => setAdjust({ ...adjust, max_per_trade: v })}
                title="Never two on the same day. Exits stay measured against the first credit."
              />
            </div>
          </details>
        )}

        {!isCrypto && (
        <details className="ob-more">
          <summary>
            Costs<span className="ob-sum">
              {slippage}% slippage · ₹{brokerage} an order
            </span>
          </summary>
          <div className="ob-grid">
            <label className="ob-field" title="Charged on every fill, against you">
              <span>Slippage, % of premium</span>
              <input type="number" min={0} step={0.05} value={slippage}
                onChange={(e) => setSlippage(Math.max(0, Number(e.target.value)))} />
            </label>
            <label className="ob-field">
              <span>At least, ₹</span>
              <input type="number" min={0} step={0.05} value={minSlip}
                onChange={(e) => setMinSlip(Math.max(0, Number(e.target.value)))} />
            </label>
            <label className="ob-field" title="Each leg, in and out. Taxes are added at each day's rates.">
              <span>Brokerage, ₹ an order</span>
              <input type="number" min={0} value={brokerage}
                onChange={(e) => setBrokerage(Math.max(0, Number(e.target.value)))} />
            </label>
          </div>
        </details>
        )}

        <div className="ob-go">
          {!timesOk && <span className="ob-error">Exit must be after entry.</span>}
          {error && <span className="ob-error">{error}</span>}
          {isCrypto ? (
            <button className="ob-run" disabled title="Save it, then open it in the live strategy builder to paper trade it">
              Paper trade
            </button>
          ) : (
            <button className="ob-run" onClick={run} disabled={!canRun}>
              {running ? `Running… ${elapsed} s` : "Run backtest"}
            </button>
          )}
        </div>
      </section>

      {!isCrypto && result && finished && (
        <OptResult result={result} stale={running} finishedAt={finished.at} seconds={finished.seconds} />
      )}
    </main>
  );
}

function Join({ value, onChange }: { value: "all" | "any"; onChange: (v: "all" | "any") => void }) {
  return (
    <select value={value} onChange={(e) => onChange(e.target.value as "all" | "any")} aria-label="How conditions combine">
      <option value="all">all of these</option>
      <option value="any">any of these</option>
    </select>
  );
}

function Points({
  label,
  value,
  onChange,
}: {
  label: string;
  value: number;
  onChange: (v: number) => void;
}) {
  return (
    <label className="ob-field">
      <span>{label}</span>
      <input type="number" min={0} step={25} value={value}
        onChange={(e) => onChange(Math.max(0, Number(e.target.value)))} />
    </label>
  );
}

/** A small whole number: strikes, or a count. */
function Count({
  label,
  value,
  min,
  onChange,
  title,
}: {
  label: string;
  value: number;
  min: number;
  onChange: (v: number) => void;
  title?: string;
}) {
  return (
    <label className="ob-field" title={title}>
      <span>{label}</span>
      <input
        type="number"
        min={min}
        max={10}
        value={value}
        onChange={(e) => onChange(Math.max(min, Math.min(10, Math.round(Number(e.target.value)))))}
      />
    </label>
  );
}

const same = (a: string[], b: string[]) => a.length === b.length && a.every((x) => b.includes(x));

function Range({
  label,
  lo,
  hi,
  onChange,
  title,
}: {
  label: string;
  lo: number | null;
  hi: number | null;
  onChange: (lo: number | null, hi: number | null) => void;
  title?: string;
}) {
  const read = (text: string) => (text === "" ? null : Number(text));
  return (
    <div className="ob-field ob-range" title={title}>
      <span>{label}</span>
      <div>
        <input type="number" step="any" placeholder="from" value={lo ?? ""}
          onChange={(e) => onChange(read(e.target.value), hi)} aria-label={`${label} from`} />
        <input type="number" step="any" placeholder="to" value={hi ?? ""}
          onChange={(e) => onChange(lo, read(e.target.value))} aria-label={`${label} to`} />
      </div>
    </div>
  );
}

/** A whole-position level: rupees, or a share of the credit taken in. */
function Limit({
  label,
  value,
  onChange,
  currency,
}: {
  label: string;
  value: { value: number | null; unit: "rs" | "credit" };
  onChange: (v: { value: number | null; unit: "rs" | "credit" }) => void;
  currency: string;
}) {
  return (
    <div
      className="ob-field"
      title={value.unit === "credit" ? `Of the credit this trade took in: a ${currency}10,000 credit at 50% is ${currency}5,000` : undefined}
    >
      <span>{label}</span>
      <div className="ob-limit">
        <input
          type="number"
          min={0}
          step={value.unit === "rs" ? 500 : 10}
          placeholder="off"
          value={value.value ?? ""}
          onChange={(e) =>
            onChange({ ...value, value: e.target.value === "" ? null : Math.max(0, Number(e.target.value)) })
          }
          aria-label={label}
        />
        <select
          value={value.unit}
          onChange={(e) => onChange({ ...value, unit: e.target.value as "rs" | "credit" })}
          aria-label={`${label} in`}
        >
          <option value="rs">{currency}</option>
          <option value="credit">% of credit</option>
        </select>
      </div>
    </div>
  );
}
