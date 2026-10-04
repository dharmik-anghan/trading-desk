import type { SimMoment } from "../../api";
import { compact, num, signed } from "../../format";
import { PayoffChart } from "../PayoffChart";

const tone = (v: number | null) => (v === null ? "" : v > 0 ? "up" : v < 0 ? "dn" : "");

/** What the included legs make: now, at the nearest expiry, and the odds of it. */
export function SimPayoff({
  moment,
  dp = 0,
  marginNote,
}: {
  moment: SimMoment;
  /** Decimals on money: none for rupees, two for a coin's USDT. */
  dp?: number;
  /** What the margin figure is, where it is not NSE's SPAN and exposure. */
  marginNote?: string;
}) {
  const p = moment.payoff;
  const hasCurve = p.expiry_curve.length > 1;
  const margin = p.span + p.exposure;
  /** A figure as a share of the margin, the way it reads against capital. */
  const ofMargin = (v: number) =>
    margin > 0 ? <small>{` (${v >= 0 ? "+" : ""}${((v / margin) * 100).toFixed(2)}%)`}</small> : null;
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
          <dt title={`Gross ${signed(p.pnl, dp)} · charges and slippage ${signed(-p.charges, dp)}`}>
            Net P&amp;L
          </dt>
          <dd className={tone(p.net)}>
            {signed(p.net, dp)}
            {ofMargin(p.net)}
          </dd>
        </div>
        {p.charges > 0 && (
          <div>
            <dt title="Charges on every fill and slippage, an open leg's exit at its last price included">
              Charges
            </dt>
            <dd className="dn">{signed(-p.charges, dp)}</dd>
          </div>
        )}
        {p.realised !== 0 && (
          <div>
            <dt>Booked</dt>
            <dd className={tone(p.realised)}>{signed(p.realised, dp)}</dd>
          </div>
        )}
        <div>
          <dt
            title={
              marginNote ??
              `What NSE's rules today would block for the open legs: SPAN ${num(p.span, 0)} + exposure ${num(p.exposure, 0)}`
            }
          >
            Est. margin
          </dt>
          <dd>{margin > 0 ? compact(margin) : "—"}</dd>
        </div>
        <div>
          <dt>Max profit</dt>
          <dd className={p.profit_unlimited ? "up" : tone(p.max_profit)}>
            {!hasCurve ? "—" : p.profit_unlimited ? "Unlimited" : signed(p.max_profit ?? 0, dp)}
            {hasCurve && !p.profit_unlimited && ofMargin(p.max_profit ?? 0)}
          </dd>
        </div>
        <div>
          <dt>Max loss</dt>
          <dd className={p.loss_unlimited ? "dn" : tone(p.max_loss)}>
            {!hasCurve ? "—" : p.loss_unlimited ? "Unlimited" : signed(p.max_loss ?? 0, dp)}
          </dd>
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
