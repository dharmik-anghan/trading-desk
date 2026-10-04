import { getQuotes, INDIA_VIX, UNDERLYINGS, WATCHLIST } from "../api";
import { num, pct } from "../format";
import { useLive } from "../hooks/useLive";
import type { Route } from "../useRoute";

interface Props {
  onGo: (route: Route) => void;
  /** Whether the perpetuals venue is configured, so a dead card is not offered. */
  cryptoReady: boolean;
}

/** One card's full pitch, kept off the page and surfaced only as a tooltip. */
const DESC: Record<string, string> = {
  options:
    "NIFTY, BANKNIFTY and the rest through Fyers. Structures you hold, their greeks and payoff, the chain, and the calendar.",
  crypto:
    "Bitcoin, gold and crude as perpetuals on Shark. Streamed prices, positions with their liquidation distance, and orders.",
  rotation:
    "Which sectors are leading, improving, weakening and lagging against the index — and which way each one is travelling.",
  backtesting:
    "What a rule would have done. Reads the bars the desks have stored, so a run can only cover history that actually exists.",
  "option-backtesting":
    "An option strategy, built leg by leg and saved, then replayed minute by minute over years of real NIFTY contracts.",
  "option-simulator":
    "Any minute of the stored NIFTY option history, replayed: the chain as it stood, legs traded by hand, stops and targets filled as time moves on.",
  live:
    "A template or a saved strategy placed on the live chain - NSE indices through Fyers, BTC and ETH on Shark - seen as a payoff, then paper traded. Stops and targets are watched by the server.",
  preopen:
    "Where every F&O stock and NIFTY were set to open, from NSE's 09:00 auction — recorded each morning, because NSE only shows the latest one.",
};

/**
 * Where to start.
 *
 * Three desks rather than one screen with a switch, because they are three different
 * jobs: watching structures you hold, trading a leveraged book, and asking what a
 * rule would have done. The first two are live and the third reads the same bars
 * they leave behind.
 *
 * The artwork is drawn here rather than downloaded. A stock photograph of a trading
 * floor would be somebody else's work with an unclear licence, baked into this
 * repository and fetched over the network - and it would say nothing. These say what
 * each desk is: a payoff kink, a candle series, an equity curve.
 */
export function Home({ onGo, cryptoReady }: Props) {
  return (
    <main className="home">
      <Ticker />

      <h1>Desk</h1>

      <div className="cards">
        <button
          className="card"
          data-accent="you"
          onClick={() => onGo("options")}
          title={DESC.options}
        >
          <PayoffMark />
          <h2>Options</h2>
          <p>Live structures, greeks and payoff via Fyers.</p>
          <span className="go">Open the options desk</span>
        </button>

        <button
          className="card"
          data-accent="mkt"
          onClick={() => onGo("crypto")}
          disabled={!cryptoReady}
          title={DESC.crypto}
        >
          <CandlesMark />
          <h2>Crypto &amp; commodities</h2>
          <p>{cryptoReady ? "Perpetuals via Shark — price, liquidation, orders." : "Needs SHARK_API_KEY in .env"}</p>
          <span className="go">
            {cryptoReady ? "Open the crypto desk" : "Needs SHARK_API_KEY in .env"}
          </span>
        </button>

        <button
          className="card"
          data-accent="i3"
          onClick={() => onGo("rotation")}
          title={DESC.rotation}
        >
          <RotationMark />
          <h2>Rotation</h2>
          <p>Sectors against the index, and which way each is moving.</p>
          <span className="go">Open the rotation graph</span>
        </button>

        <button
          className="card"
          data-accent="i2"
          onClick={() => onGo("backtesting")}
          title={DESC.backtesting}
        >
          <EquityMark />
          <h2>Backtesting</h2>
          <p>What a rule would have done, from the bars already stored.</p>
          <span className="go">Open backtesting</span>
        </button>

        <button
          className="card"
          data-accent="up"
          onClick={() => onGo("option-backtesting")}
          title={DESC["option-backtesting"]}
        >
          <StraddleMark />
          <h2>Strategy builder</h2>
          <p>Build and save an option strategy, and replay it over years of NIFTY contracts.</p>
          <span className="go">Open the builder</span>
        </button>

        <button
          className="card"
          data-accent="i2"
          onClick={() => onGo("option-simulator")}
          title={DESC["option-simulator"]}
        >
          <ReplayMark />
          <h2>Simulator</h2>
          <p>Trade any past minute of NIFTY options by hand, and step time forward.</p>
          <span className="go">Open the simulator</span>
        </button>

        <button
          className="card"
          data-accent="you"
          onClick={() => onGo("live")}
          title={DESC.live}
        >
          <LiveMark />
          <h2>Live strategy builder</h2>
          <p>A strategy on the live chain, NSE or crypto, paper traded at the market.</p>
          <span className="go">Open the live builder</span>
        </button>

        <button
          className="card"
          data-accent="i4"
          onClick={() => onGo("preopen")}
          title={DESC.preopen}
        >
          <AuctionMark />
          <h2>Pre-open</h2>
          <p>Where every F&amp;O stock opened, recorded each morning at 09:00.</p>
          <span className="go">Open the pre-open record</span>
        </button>
      </div>
    </main>
  );
}

/**
 * A thin scrolling strip of the same watchlist the desks trade off, polled
 * slowly since it is ambient context rather than something to act on here.
 * The dot only lights while a poll has actually landed recently - it reports
 * the feed, it doesn't perform one.
 */
function Ticker() {
  const quotes = useLive(() => getQuotes(WATCHLIST), 15000, [], false, 0);
  const rows = [...UNDERLYINGS, INDIA_VIX];
  const items = rows
    .map((r) => {
      const q = quotes.data?.[r.id];
      if (!q) return null;
      const change = q.prev_close ? ((q.ltp - q.prev_close) / q.prev_close) * 100 : 0;
      return { name: r.name as string, ltp: q.ltp, change };
    })
    .filter((x) => x !== null);

  if (items.length === 0) return <div className="ticker" aria-hidden="true" />;

  const fresh = quotes.at !== null && !quotes.error && Date.now() - quotes.at < 20000;
  const line = items.concat(items); // doubled, for a seamless loop
  return (
    <div className="ticker">
      <span className={`ticker-live${fresh ? " on" : ""}`}>
        <i />
        LIVE
      </span>
      <div className="ticker-track">
        <div className="ticker-row">
          {line.map((it, i) => (
            <span className="ticker-item" key={i}>
              <b>{it.name}</b>
              <span>{num(it.ltp, it.ltp >= 1000 ? 0 : 2)}</span>
              <span className={it.change >= 0 ? "up" : "dn"}>{pct(it.change)}</span>
            </span>
          ))}
        </div>
      </div>
    </div>
  );
}

/**
 * A payoff diagram: an iron condor's tent, the way the real payoff panel
 * draws one. Flat top shaded as the profit zone, the two kinks as the short
 * strikes, breakevens dropped as dashed verticals down to the strike axis.
 */
function PayoffMark() {
  return (
    <svg viewBox="0 0 220 104" className="mark" aria-hidden="true">
      <line x1="0" y1="80" x2="220" y2="80" className="markaxis" />
      {[18, 54, 90, 130, 166, 202].map((x) => (
        <line key={x} x1={x} y1="78" x2={x} y2="83" className="marktick" />
      ))}
      <path
        d="M4 96 L58 96 L92 26 L128 26 L216 62 L216 96 L58 96 Z"
        className="markfill you"
      />
      <path d="M4 96 L58 96 L92 26 L128 26 L216 62" className="markline you" />
      <line x1="92" y1="26" x2="92" y2="80" className="markdash" />
      <line x1="128" y1="26" x2="128" y2="80" className="markdash" />
      <circle cx="92" cy="26" r="3" className="markdot" />
      <circle cx="128" cy="26" r="3" className="markdot" />
    </svg>
  );
}

/** Candles with their volume underneath: what a leveraged book is watched on. */
function CandlesMark() {
  const bars: [number, number, number, number][] = [
    // x, top, bottom, rising
    [14, 30, 62, 1],
    [34, 20, 46, 1],
    [54, 38, 70, 0],
    [74, 14, 40, 1],
    [94, 28, 60, 0],
    [114, 10, 34, 1],
    [134, 24, 52, 0],
    [154, 6, 28, 1],
    [174, 18, 44, 1],
    [194, 12, 38, 0],
  ];
  return (
    <svg viewBox="0 0 220 104" className="mark" aria-hidden="true">
      <line x1="0" y1="80" x2="220" y2="80" className="markaxis" />
      {bars.map(([x, top, bottom, rising]) => (
        <g key={x} className={rising ? "up" : "dn"}>
          <line x1={x} x2={x} y1={top - 7} y2={bottom + 7} className="markwick" />
          <rect x={x - 6} y={top} width="12" height={bottom - top} className="markbody" />
          <rect x={x - 5} y={88 + (x % 40) / 4} width="10" height={14 - (x % 40) / 4} className="markbody qty" />
        </g>
      ))}
    </svg>
  );
}

/** A relative-rotation quadrant, with two sectors' tails travelling through it. */
function RotationMark() {
  return (
    <svg viewBox="0 0 220 104" className="mark" aria-hidden="true">
      <rect x="110" y="4" width="106" height="46" className="markfill up" />
      <rect x="4" y="54" width="106" height="46" className="markfill dn" />
      <line x1="110" y1="4" x2="110" y2="100" className="markgrid" />
      <line x1="4" y1="52" x2="216" y2="52" className="markgrid" />
      <path d="M40 88 C70 84, 92 68, 104 46 C112 32, 120 22, 132 16" className="markline mkt" />
      <circle cx="40" cy="88" r="2.5" className="markdot dim" />
      <circle cx="132" cy="16" r="3.5" className="markdot" />
      <path d="M160 94 C172 80, 176 62, 172 44" className="markline you" />
      <circle cx="160" cy="94" r="2.5" className="markdot dim" />
      <circle cx="172" cy="44" r="3.5" className="markdot you" />
    </svg>
  );
}

/** An equity curve with its drawdown shaded in, the way a run's summary draws it. */
function EquityMark() {
  return (
    <svg viewBox="0 0 220 104" className="mark" aria-hidden="true">
      <line x1="0" y1="86" x2="220" y2="86" className="markaxis" />
      <path
        d="M4 72 L26 60 L46 66 L64 44 L82 52 L100 30 L118 38 L136 20 L154 26 L172 12 L216 6 L216 86 L4 86 Z"
        className="markfill mkt"
      />
      <path
        d="M4 72 L26 60 L46 66 L64 44 L82 52 L100 30 L118 38 L136 20 L154 26 L172 12 L216 6"
        className="markline mkt"
      />
      <path d="M46 66 L64 44" className="markline dn thick" />
    </svg>
  );
}

/** An auction book: buyers and sellers stacked either side of the price they met at. */
function AuctionMark() {
  const levels: [number, number, number][] = [
    // y, buy width, sell width
    [14, 0, 58],
    [30, 0, 34],
    [46, 42, 50],
    [62, 64, 0],
    [78, 30, 0],
    [94, 14, 0],
  ];
  return (
    <svg viewBox="0 0 220 104" className="mark" aria-hidden="true">
      {levels.map(([y, buy, sell]) => (
        <g key={y}>
          {buy > 0 && (
            <rect x={108 - buy} y={y - 5} width={buy} height="10" className="markbody up" />
          )}
          {sell > 0 && <rect x={112} y={y - 5} width={sell} height="10" className="markbody dn" />}
        </g>
      ))}
      <line x1="110" y1="2" x2="110" y2="102" className="markline mkt thick" />
      <path d="M110 2 L104 12 L116 12 Z" className="markarrow" />
    </svg>
  );
}

/** A short straddle's tent over the minute line it is replayed on. */
/** A price path stopped part-way, with the rest still to be played. */
function ReplayMark() {
  const path = [52, 46, 50, 40, 44, 34, 38, 30, 36, 28, 32, 40, 36, 46, 42, 50, 44, 38, 42, 34];
  const step = 220 / (path.length - 1);
  const at = 10;
  const pts = path.map((v, i) => `${i === 0 ? "M" : "L"}${i * step} ${v + 20}`);
  return (
    <svg viewBox="0 0 220 104" className="mark" aria-hidden="true">
      <line x1="0" y1="86" x2="220" y2="86" className="markaxis" />
      <path d={pts.slice(0, at + 1).join(" ")} className="markline you" />
      <path d={["M" + pts[at].slice(1), ...pts.slice(at + 1)].join(" ")} className="markline mkt thin" strokeDasharray="3 4" />
      <line x1={at * step} y1="8" x2={at * step} y2="86" className="markaxis" />
      <circle cx={at * step} cy={path[at] + 20} r="3" className="markdot" />
    </svg>
  );
}

/** A condor's payoff, and where the market stands on it now. */
function LiveMark() {
  return (
    <svg viewBox="0 0 220 104" className="mark" aria-hidden="true">
      <line x1="0" y1="70" x2="220" y2="70" className="markaxis" />
      <path d="M4 96 L56 96 L84 30 L136 30 L164 96 L216 96 L216 70 L4 70 Z" className="markfill up" />
      <path d="M4 96 L56 96 L84 30 L136 30 L164 96 L216 96" className="markline you" />
      <line x1="118" y1="8" x2="118" y2="100" className="markaxis" strokeDasharray="3 4" />
      <circle cx="118" cy="30" r="4" className="markdot" />
    </svg>
  );
}

function StraddleMark() {
  const minutes = [
    8, 14, 10, 18, 12, 22, 16, 28, 20, 34, 24, 40, 30, 46, 34, 50, 38, 54, 42, 58,
  ];
  const step = 220 / (minutes.length - 1);
  const line = minutes.map((v, i) => `${i === 0 ? "M" : "L"}${i * step} ${74 - v}`).join(" ");
  return (
    <svg viewBox="0 0 220 104" className="mark" aria-hidden="true">
      <line x1="0" y1="86" x2="220" y2="86" className="markaxis" />
      <path d="M4 100 L110 16 L216 100 L216 86 L110 86 L4 86 Z" className="markfill dn" />
      <path d="M4 100 L110 16 L216 100" className="markline you" />
      <circle cx="110" cy="16" r="3" className="markdot" />
      <path d={line} className="markline mkt thin" />
    </svg>
  );
}
