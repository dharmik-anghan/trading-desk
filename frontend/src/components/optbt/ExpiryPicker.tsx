import type { OptbtExpiryChoice } from "../../api";
import { NTH } from "./legs";

interface Props {
  value: OptbtExpiryChoice;
  onChange: (value: OptbtExpiryChoice) => void;
  /** Offer "daily": a venue that lists expiries between its weeklies. */
  daily?: boolean;
}

/** Weekly · 1st · at least 1 day left - the expiry every leg trades unless it names its own. */
export function ExpiryPicker({ value, onChange, daily = false }: Props) {
  const set = <K extends keyof OptbtExpiryChoice>(key: K, v: OptbtExpiryChoice[K]) =>
    onChange({ ...value, [key]: v });
  const days = value.series === "days";

  return (
    <div className="ob-field">
      <span>Expiry</span>
      <div className="ob-strike">
        <select
          value={value.series}
          onChange={(e) => set("series", e.target.value as OptbtExpiryChoice["series"])}
          aria-label="Expiry series"
        >
          {(daily || value.series === "daily") && <option value="daily">Daily</option>}
          <option value="weekly">Weekly</option>
          <option value="monthly">Monthly</option>
          <option value="days">≈ days out</option>
        </select>
        {days ? (
          <input
            type="number"
            min={1}
            max={120}
            value={value.days}
            onChange={(e) => set("days", Math.max(1, Number(e.target.value)))}
            aria-label="Calendar days to expiry"
            title="The monthly expiry nearest this many calendar days out"
          />
        ) : (
          <select
            value={value.nth}
            onChange={(e) => set("nth", Number(e.target.value))}
            aria-label="Which expiry"
            title="Counted nearest first, after any with too few days left are passed over"
          >
            {NTH.map((label, i) => (
              <option key={label} value={i + 1}>
                {label}
              </option>
            ))}
          </select>
        )}
        <label
          className="ob-left"
          title="Trading days an expiry must have left to be taken. 1 skips it on its own day and takes the next; 2 skips the day before too."
        >
          <input
            type="number"
            min={0}
            max={10}
            value={value.min_left}
            onChange={(e) => set("min_left", Math.min(10, Math.max(0, Number(e.target.value))))}
            aria-label="Days left at least"
          />
          <span>days left min</span>
        </label>
      </div>
    </div>
  );
}
