import type { SimMoment } from "../../api";
import { num, signed } from "../../format";
import { PayoffChart } from "../PayoffChart";

const tone = (v: number | null) => (v === null ? "" : v > 0 ? "up" : v < 0 ? "dn" : "");

/** What the included legs make: now, at the nearest expiry, and the odds of it. */
export function SimPayoff({ moment }: { moment: SimMoment }) {
  const p = moment.payoff;
  const hasCurve = p.expiry_curve.length > 1;
  const nearest = moment.legs
    .filter((l) => l.enabled && l.status === "open")
    .map((l) => l.expiry)
    .sort()[0];
  const days = nearest
    ? Math.max(0, (new Date(`${nearest}T15:30:00`).getTime() - new Date(moment.at).getTime()) / 86400000)
    : null;
  return (
    <section className="sim-payoff" aria-label="Payoff">
      <dl className="sim-stats">
        <div>
          <dt>P&amp;L</dt>
          <dd className={tone(p.pnl)}>{signed(p.pnl)}</dd>
        </div>
        {p.realised !== 0 && (
          <div>
            <dt>Booked</dt>
            <dd className={tone(p.realised)}>{signed(p.realised)}</dd>
          </div>
        )}
        <div>
          <dt>Max profit</dt>
          <dd className="up">
            {!hasCurve ? "—" : p.profit_unlimited ? "Unlimited" : signed(p.max_profit ?? 0)}
          </dd>
        </div>
        <div>
          <dt>Max loss</dt>
          <dd className="dn">{!hasCurve ? "—" : p.loss_unlimited ? "Unlimited" : signed(p.max_loss ?? 0)}</dd>
        </div>
        <div>
          <dt title="Chance spot finishes the nearest expiry where this makes money, at the ATM implied volatility">
            POP
          </dt>
          <dd>{p.pop === null ? "—" : `${Math.round(p.pop * 100)}%`}</dd>
        </div>
        <div>
          <dt>Breakevens</dt>
          <dd>{p.breakevens.length ? p.breakevens.map((b) => num(b, 0)).join(" · ") : "—"}</dd>
        </div>
      </dl>
      <div className="sim-chart">
        {hasCurve ? (
          <PayoffChart
            curve={p.expiry_curve.map(([spot, payoff]) => ({ spot, payoff }))}
            todayCurve={p.today_curve.map(([spot, payoff]) => ({
              spot,
              payoff,
            }))}
            spot={moment.spot}
            breakevens={p.breakevens}
            daysToExpiry={days}
            height={300}
            zoomable
          />
        ) : (
          <p className="sim-empty">The payoff draws once a leg is open.</p>
        )}
      </div>
    </section>
  );
}
