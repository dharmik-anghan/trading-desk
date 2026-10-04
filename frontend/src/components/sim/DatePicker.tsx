import { useEffect, useRef, useState } from "react";
import { getSimCalendar } from "../../api";
import type { SimMonth } from "../../api";

interface Props {
  underlying: string;
  /** The moment shown, "YYYY-MM-DDTHH:MM:SS". */
  value: string;
  label: string;
  onPick: (at: string) => void;
}

const WEEK = ["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"];
const HOURS = [9, 10, 11, 12, 13, 14, 15];
const MINUTES = Array.from({ length: 60 }, (_, i) => i);
/** The earliest day the fetch will ask Fyers for. */
const EARLIEST = "2019-01-01";

const pad = (n: number) => String(n).padStart(2, "0");
const iso = (y: number, m: number, d: number) => `${y}-${pad(m + 1)}-${pad(d)}`;

/** Minutes the session trades in an hour: 09:15-09:59, then whole hours, then 15:00-15:29. */
const minutesIn = (h: number) => MINUTES.filter((m) => (h === 9 ? m >= 15 : h === 15 ? m <= 29 : true));

/**
 * A month at a time, as StockMojo draws it: expiry days marked, holidays
 * greyed, nothing after today. A day the store does not hold yet is still
 * pickable - it is fetched when opened.
 */
export function DatePicker({ underlying, value, label, onPick }: Props) {
  const [open, setOpen] = useState(false);
  const [view, setView] = useState(() => ({
    y: Number(value.slice(0, 4)),
    m: Number(value.slice(5, 7)) - 1,
  }));
  const [day, setDay] = useState(value.slice(0, 10));
  const [hour, setHour] = useState(Number(value.slice(11, 13)));
  const [minute, setMinute] = useState(Number(value.slice(14, 16)));
  const [month, setMonth] = useState<SimMonth | null>(null);
  const box = useRef<HTMLDivElement>(null);

  // Opening starts from the moment shown, not from wherever it was left.
  useEffect(() => {
    if (!open) return;
    setView({ y: Number(value.slice(0, 4)), m: Number(value.slice(5, 7)) - 1 });
    setDay(value.slice(0, 10));
    setHour(Number(value.slice(11, 13)));
    setMinute(Number(value.slice(14, 16)));
  }, [open, value]);

  useEffect(() => {
    if (!open) return;
    let alive = true;
    setMonth(null);
    void getSimCalendar(underlying, `${view.y}-${pad(view.m + 1)}`)
      .then((m) => alive && setMonth(m))
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [open, underlying, view]);

  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false);
    };
    const esc = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", away);
      document.removeEventListener("keydown", esc);
    };
  }, [open]);

  const today = new Date();
  const todayIso = iso(today.getFullYear(), today.getMonth(), today.getDate());
  const sessions = new Set(month?.sessions ?? []);
  const expiries = new Set(month?.expiries ?? []);

  const kind = (d: string, weekday: number): "off" | "holiday" | "open" => {
    if (d >= todayIso || d < EARLIEST || weekday === 0 || weekday === 6) return "off";
    // Inside the span the store holds, a weekday without a session was a holiday.
    if (month?.first && month.last && d >= month.first && d <= month.last && !sessions.has(d))
      return "holiday";
    return "open";
  };

  const first = new Date(view.y, view.m, 1).getDay();
  const length = new Date(view.y, view.m + 1, 0).getDate();
  const cells: (number | null)[] = [...Array(first).fill(null), ...Array.from({ length }, (_, i) => i + 1)];
  while (cells.length % 7) cells.push(null);

  const shift = (months: number) =>
    setView((v) => {
      const t = v.y * 12 + v.m + months;
      return { y: Math.floor(t / 12), m: t % 12 };
    });
  const pickHour = (h: number) => {
    setHour(h);
    const allowed = minutesIn(h);
    if (!allowed.includes(minute)) setMinute(allowed[0]);
  };

  return (
    <div className="sim-when" ref={box}>
      <button className="sim-when-label" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        {label}
        <svg viewBox="0 0 16 16" aria-hidden="true">
          <rect x="2" y="3" width="12" height="11" rx="1.5" />
          <path d="M2 6.5h12M5.5 1.5v3M10.5 1.5v3" />
        </svg>
      </button>
      {open && (
        <div className="sim-cal" role="dialog" aria-label="Go to a moment">
          <div className="sim-cal-days">
            <header>
              <button onClick={() => shift(-12)} aria-label="A year back">
                «
              </button>
              <button onClick={() => shift(-1)} aria-label="A month back">
                ‹
              </button>
              <b>
                {new Date(view.y, view.m, 1).toLocaleDateString("en-IN", { month: "short", year: "numeric" })}
              </b>
              <button onClick={() => shift(1)} aria-label="A month on">
                ›
              </button>
              <button onClick={() => shift(12)} aria-label="A year on">
                »
              </button>
            </header>
            <div className="sim-cal-grid">
              {WEEK.map((w) => (
                <span key={w} className="wk">
                  {w}
                </span>
              ))}
              {cells.map((n, i) => {
                if (n === null) return <span key={`x${i}`} />;
                const d = iso(view.y, view.m, n);
                const k = kind(d, i % 7);
                const cls = [k, expiries.has(d) ? "exp" : "", d === day ? "on" : ""].join(" ");
                return (
                  <button
                    key={d}
                    className={cls}
                    disabled={k !== "open"}
                    onClick={() => setDay(d)}
                    title={k === "holiday" ? "Holiday" : expiries.has(d) ? "Expiry" : undefined}
                  >
                    {n}
                  </button>
                );
              })}
            </div>
            <footer>
              <span>
                <i className="exp" /> Expiry
              </span>
              <span>
                <i className="holiday" /> Holiday
              </span>
            </footer>
          </div>
          <div className="sim-cal-time">
            <b>
              {pad(hour)}:{pad(minute)}
            </b>
            <div>
              <ul aria-label="Hour">
                {HOURS.map((h) => (
                  <li key={h}>
                    <button className={h === hour ? "on" : ""} onClick={() => pickHour(h)}>
                      {pad(h)}
                    </button>
                  </li>
                ))}
              </ul>
              <ul aria-label="Minute">
                {minutesIn(hour).map((m) => (
                  <li key={m}>
                    <button className={m === minute ? "on" : ""} onClick={() => setMinute(m)}>
                      {pad(m)}
                    </button>
                  </li>
                ))}
              </ul>
            </div>
            <button
              className="sim-cal-ok"
              onClick={() => {
                setOpen(false);
                onPick(`${day}T${pad(hour)}:${pad(minute)}:00`);
              }}
            >
              OK
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
