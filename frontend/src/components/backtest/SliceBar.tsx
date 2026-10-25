import type { BacktestTrade } from "../../api";
import { NO_SLICE, WEEKDAYS, ZONES, endingsIn, figures, isSliced, yearsIn } from "./slice";
import type { Slice } from "./slice";

interface Props {
  value: Slice;
  onChange: (next: Slice) => void;
  /** Every trade the run took; the options are drawn from these. */
  all: BacktestTrade[];
  shown: BacktestTrade[];
}

const money = (v: number) =>
  v.toLocaleString(undefined, { maximumFractionDigits: 0, minimumFractionDigits: 0 });

/** Filters over the trades already taken. Nothing here runs the backtest again. */
export function SliceBar({ value: s, onChange, all, shown }: Props) {
  const set = <K extends keyof Slice>(k: K, v: Slice[K]) => onChange({ ...s, [k]: v });
  const toggle = (day: string) =>
    set("weekdays", s.weekdays.includes(day) ? s.weekdays.filter((d) => d !== day) : [...s.weekdays, day]);
  const f = figures(shown);

  return (
    <div className="ob-filter" role="toolbar" aria-label="Filter trades">
      <Pick
        label="Side"
        value={s.side}
        onChange={(v) => set("side", v as Slice["side"])}
        options={[["", "Both"], ["long", "Long"], ["short", "Short"]]}
      />
      <Pick
        label="Session"
        value={s.zone}
        onChange={(v) => set("zone", v)}
        options={[["", "Any"], ...ZONES]}
      />
      <div className="ob-seg days" aria-label="Weekday">
        {WEEKDAYS.map((d) => (
          <button
            key={d}
            className={s.weekdays.includes(d) ? "on" : ""}
            aria-pressed={s.weekdays.includes(d)}
            onClick={() => toggle(d)}
          >
            {d}
          </button>
        ))}
      </div>
      <Pick
        label="Year"
        value={s.year}
        onChange={(v) => set("year", v)}
        options={[["", "All"], ...yearsIn(all).map((y) => [y, y] as [string, string])]}
      />
      <Pick
        label="Ended by"
        value={s.ended}
        onChange={(v) => set("ended", v)}
        options={[["", "Any"], ...endingsIn(all).map((e) => [e, e] as [string, string])]}
      />
      <span className="ob-count">
        {shown.length === all.length
          ? `${all.length.toLocaleString()} trades`
          : `${shown.length.toLocaleString()} of ${all.length.toLocaleString()}`}
        {f.trades > 0 && (
          <>
            {" · "}
            {Math.round((100 * f.wins) / f.trades)}% won · gross {money(f.gross)} · costs{" "}
            {money(f.costs)} ·{" "}
            <b className={f.net >= 0 ? "up" : "dn"}>net {money(f.net)}</b>
          </>
        )}
      </span>
      {isSliced(s) && (
        <button className="ob-reset" onClick={() => onChange(NO_SLICE)}>
          Reset
        </button>
      )}
    </div>
  );
}

function Pick({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  options: [string, string][];
}) {
  return (
    <label className={`ob-pick${value !== options[0][0] ? " on" : ""}`}>
      <span>{label}</span>
      <select value={value} onChange={(e) => onChange(e.target.value)}>
        {options.map(([v, text]) => (
          <option key={v} value={v}>
            {text}
          </option>
        ))}
      </select>
    </label>
  );
}
