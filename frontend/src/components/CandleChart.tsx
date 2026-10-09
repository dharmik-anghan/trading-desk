import { useEffect, useMemo, useRef, useState } from "react";
import {
  AreaSeries,
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  HistogramSeries,
  LineSeries,
  LineStyle,
  createChart,
  createSeriesMarkers,
} from "lightweight-charts";
import type {
  IChartApi,
  IPriceLine,
  ISeriesApi,
  LogicalRange,
  SeriesMarker,
  Time,
  UTCTimestamp,
  WhitespaceData,
} from "lightweight-charts";
import type { Candle, IndicatorLine } from "../api";
import { BUILDUP_LABEL, alignOi } from "../charts/buildup";
import type { OiAtBar } from "../charts/buildup";
import { OiProfile, fade, profileWidth } from "../charts/oiProfile";
import type { OiRow } from "../charts/oiProfile";
import { compact, int } from "../format";

interface Props {
  candles: readonly Candle[];
  /** Identifies the series. The pan/zoom window resets when this changes and
      survives when it does not - so a refresh that adds a bar leaves the view
      where you put it, while switching symbol or timeframe starts fresh. */
  seriesId: string;
  /** Live price, drawn as the current level so the chart agrees with the tile. */
  last: number | null;
  /** Decimal places the venue quotes in, so the axis invents no precision. */
  dp: number;
  /** Height of the price pane, before any oscillator panes. */
  height?: number;
  /** Things drawn on top of the price: a backtest's entry and exit, its stop and
      target, and the stretch of time the position was held. Optional, because a
      live chart has none of them. */
  overlay?: Overlay;
  /** Indicators that are not prices, one pane each under the candles, sharing
      their time axis and crosshair. `colour` indexes the indicator palette. */
  oscillators?: readonly { line: IndicatorLine; colour: number }[];
}

export interface Overlay {
  /** Horizontal lines at a price, each with a short label on the axis. */
  levels?: { price: number; label: string; kind: Kind }[];
  /** A point in time and price: where a position was opened or closed. */
  marks?: { at: string; price: number; kind: "entry" | "exit"; side: "long" | "short" }[];
  /** A level that existed between two moments rather than across the chart —
      a broken swing runs from where it was set to where it was taken, and
      drawing it full width states it at times it had not happened. */
  segments?: { from: string; to: string; price: number; label: string; kind: Kind }[];
  /** The stretch a position was held over, shaded. */
  band?: { from: string; to: string };
  /** Indicator lines, one value per candle, null where not yet defined. */
  lines?: { label: string; values: (number | null)[] }[];
  /** Open interest by strike, drawn against the price axis. */
  profile?: OiRow[];
  /** A future's close and OI over time, for a buildup pane under the candles. */
  futuresOi?: { at: string; close: number; oi: number; roll?: boolean }[];
}

/** `rise` and `fall` are a direction of price, drawn in the candles' colours;
    `wall` is a level the option market set, in the market hue. */
type Kind = "entry" | "exit" | "stop" | "target" | "rise" | "fall" | "wall";

/** Height of each oscillator pane, for sizing the box. */
const PANE_H = 100;
/** The price pane's share of the height against each oscillator's one. */
const PRICE_SHARE = 5;

/** Bars on screen when a series first opens. More is a wall of slivers. */
const OPEN_BARS = 200;

/** Bounded indicators keep their own bounds rather than being fitted to what
    is on screen: an RSI of 45 touching the top of its pane says the opposite
    of what an oscillator is for. */
const BOUNDED: Record<string, [number, number]> = {
  rsi: [0, 100],
  pivot_gap_rank: [0, 100],
};

/** Lines worth marking on a bounded indicator. */
const GUIDES: Record<string, number[]> = {
  rsi: [30, 70],
  pivot_gap_rank: [10, 90],
};

/** The desk's tokens, read off the page so the chart follows the theme. */
function palette() {
  const css = getComputedStyle(document.documentElement);
  const v = (name: string, fallback: string) => css.getPropertyValue(name).trim() || fallback;
  return {
    panel: v("--panel", "#ffffff"),
    grid: v("--grid", "#eceef1"),
    line: v("--line", "#dcdfe4"),
    fg: v("--fg", "#15181d"),
    dim: v("--dim", "#5d6470"),
    you: v("--you", "#5b4bd6"),
    mkt: v("--mkt", "#805706"),
    up: v("--up", "#0a6b45"),
    // Candles take the desk's own ink, not the P&L pair: up in the accent,
    // down in the quiet grey. Green and red stay for money.
    rise: v("--you", "#5b4bd6"),
    fall: v("--dim", "#5d6470"),
    dn: v("--dn", "#ae2835"),
    held: v("--selbg", "rgba(91, 75, 214, 0.1)"),
    font: v("--n", "system-ui, sans-serif"),
    ind: [0, 1, 2, 3, 4].map((i) => v(`--i${i}`, "#6b7280")),
  };
}
type Palette = ReturnType<typeof palette>;

/** Changes whenever the theme does, by switch or by the OS. */
function useThemeTick(): number {
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const bump = () => setTick((t) => t + 1);
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    media.addEventListener("change", bump);
    const watch = new MutationObserver(bump);
    watch.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    return () => {
      media.removeEventListener("change", bump);
      watch.disconnect();
    };
  }, []);
  return tick;
}

/**
 * A bar's time as the chart wants it: seconds, shifted by the local offset.
 *
 * The library labels its axis in UTC. Shifting each bar by the browser's own
 * offset makes those labels read as wall-clock time here, so a 09:15 bar says
 * 09:15 rather than 03:45.
 */
function chartTime(ms: number): UTCTimestamp {
  return (Math.floor(ms / 1000) - new Date(ms).getTimezoneOffset() * 60) as UTCTimestamp;
}

/** A change in open interest, compacted and signed. */
function change(by: number): string {
  return by > 0 ? `+${compact(by)}` : compact(by);
}

/** A time worth putting in the reading: the day and the minute, no more. */
function when(iso: string): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return iso;
  return at.toLocaleString(undefined, {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

function Reading({
  label,
  value,
  dp,
  tone,
}: {
  label: string;
  value: number;
  dp: number;
  tone?: "rise" | "fall";
}) {
  return (
    <span className="ohlc">
      <em>{label}</em>
      <b className={tone}>{value.toFixed(dp)}</b>
    </span>
  );
}

/**
 * Candles, drawn by TradingView's Lightweight Charts.
 *
 * Panning, zooming, both axes and the crosshair are the library's; what is
 * ours is the data and the overlay. Oscillators go in panes of the same chart
 * rather than separate drawings, so they share the time axis and the crosshair
 * with the candles by construction.
 *
 * Direction is drawn in the desk's accent and its grey rather than the P&L
 * pair, so green and red on this screen still only ever mean money.
 */
export function CandleChart({
  candles,
  seriesId,
  last,
  dp,
  height = 260,
  overlay,
  oscillators = [],
}: Props) {
  const box = useRef<HTMLDivElement | null>(null);
  const chart = useRef<IChartApi | null>(null);
  const price = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const shownSeries = useRef<string | null>(null);
  const hadProfile = useRef(false);
  const theme = useThemeTick();
  // The bar under the cursor, as an index into the bars drawn.
  const [hovered, setHovered] = useState<number | null>(null);
  // The price level under the cursor, for reading the OI profile.
  const [cursorPrice, setCursorPrice] = useState<number | null>(null);

  // The bars the chart can take: in order, one per moment. `orig` maps each
  // back to its place in `candles`, which is what the indicator values run on.
  const bars = useMemo(() => {
    const ms: number[] = [];
    const orig: number[] = [];
    candles.forEach((c, i) => {
      const t = Date.parse(c.at);
      if (Number.isNaN(t) || (ms.length && t <= ms[ms.length - 1])) return;
      ms.push(t);
      orig.push(i);
    });
    return { ms, orig, times: ms.map(chartTime) };
  }, [candles]);

  // Futures OI per drawn bar, for the buildup pane; empty when there is none.
  const futuresOi = overlay?.futuresOi;
  const oiBars = useMemo(
    () => (futuresOi?.length ? alignOi(bars.ms, futuresOi) : []),
    [bars.ms, futuresOi],
  );
  const oiPane = oiBars.some((b) => b !== null);

  useEffect(() => {
    const element = box.current;
    if (element === null) return;
    const made = createChart(element, {
      autoSize: true,
      crosshair: { mode: CrosshairMode.Normal },
      timeScale: { timeVisible: true, secondsVisible: false, rightOffset: 6 },
      rightPriceScale: { borderVisible: false },
    });
    made.subscribeCrosshairMove((param) => {
      const at = param.time === undefined ? null : param.logical;
      setHovered(at === undefined || at === null ? null : Math.round(at));
      const series = price.current;
      setCursorPrice(
        param.point && series ? series.coordinateToPrice(param.point.y) : null,
      );
    });
    chart.current = made;
    return () => {
      made.remove();
      chart.current = null;
      price.current = null;
    };
  }, []);

  useEffect(() => {
    const made = chart.current;
    if (made === null) return;
    const p = palette();
    made.applyOptions({
      layout: {
        background: { type: ColorType.Solid, color: p.panel },
        textColor: p.dim,
        fontFamily: p.font,
        fontSize: 10,
        panes: { separatorColor: p.grid, separatorHoverColor: fade(p.line, 0.5) },
      },
      grid: { vertLines: { color: p.grid }, horzLines: { color: p.grid } },
      crosshair: {
        vertLine: { color: p.dim, labelBackgroundColor: p.fg },
        horzLine: { color: p.dim, labelBackgroundColor: p.fg },
      },
      timeScale: { borderColor: p.grid },
    });

    // Rebuilt whole rather than patched: the overlay changes shape between
    // calls, and the library redraws a few thousand bars in no time at all.
    const keep: LogicalRange | null =
      shownSeries.current === seriesId ? made.timeScale().getVisibleLogicalRange() : null;
    for (const s of [...made.panes().flatMap((pane) => pane.getSeries())]) made.removeSeries(s);
    price.current = null;
    if (!bars.times.length) return;

    draw(made, p, candles, bars, dp, overlay, oscillators, price, oiBars);

    // Shares rather than pixels: the chart sizes itself to its box, and fixed
    // heights were handed whatever was left over - which, after a rebuild,
    // could be an oscillator taking most of the panel.
    made.panes().forEach((pane, i) => pane.setStretchFactor(i === 0 ? PRICE_SHARE : 1));
    // A new series opens on its newest bars; so does switching the OI profile
    // on or off, because the profile takes the right-hand side of the plot and
    // the newest candles should sit clear of it rather than under it.
    const hasProfile = Boolean(overlay?.profile?.length);
    const reframe = keep === null || hasProfile !== hadProfile.current;
    hadProfile.current = hasProfile;
    if (!reframe && keep !== null) {
      made.timeScale().setVisibleLogicalRange(keep);
    } else {
      shownSeries.current = seriesId;
      const n = bars.times.length;
      const shown = Math.min(n, OPEN_BARS);
      const plot = made.timeScale().width();
      const share = hasProfile && plot > 0 ? Math.min(0.6, profileWidth(plot) / plot) : 0;
      // Empty bars past the newest, enough to fill the profile's share of the plot.
      const pad = share > 0 ? Math.ceil((shown * share) / (1 - share)) + 2 : 6;
      made.timeScale().setVisibleLogicalRange({ from: n - shown, to: n + pad });
    }
  }, [candles, bars, seriesId, dp, height, overlay, oscillators, theme, oiBars]);

  // The live price, on its own: it moves every tick, and a tick should not
  // rebuild the chart. It does follow a rebuild, which removes the series it
  // was drawn on - hence the rebuild's inputs among its own.
  useEffect(() => {
    const series = price.current;
    if (series === null || last === null) return;
    const p = palette();
    series.applyOptions({ lastValueVisible: false, priceLineVisible: false });
    const line: IPriceLine = series.createPriceLine({
      price: last,
      color: p.you,
      lineWidth: 1,
      lineStyle: LineStyle.Dashed,
      axisLabelVisible: true,
      axisLabelColor: p.you,
      axisLabelTextColor: p.panel,
    });
    return () => {
      // The series may have been removed by a rebuild, taking the line with it.
      if (price.current === series) series.removePriceLine(line);
    };
  }, [last, candles, bars, seriesId, dp, height, overlay, oscillators, theme]);

  // The newest bar, moved by the live price while its period is still open, so
  // a streamed tick shows in the candle and not only in the line. The extremes
  // are kept across ticks: the polled bar only knows the high and low as of
  // its last fetch.
  const forming = useRef<{ time: number; high: number; low: number } | null>(null);
  useEffect(() => {
    const series = price.current;
    const n = bars.ms.length;
    if (series === null || last === null || n === 0) return;
    // A bar's length, as the shortest gap among the last few: weekends and
    // holidays only ever make gaps longer.
    let step = Infinity;
    for (let i = Math.max(1, n - 6); i < n; i += 1) step = Math.min(step, bars.ms[i] - bars.ms[i - 1]);
    if (!Number.isFinite(step) || Date.now() >= bars.ms[n - 1] + step) return;
    const bar = candles[bars.orig[n - 1]];
    const time = bars.times[n - 1];
    const held = forming.current?.time === time ? forming.current : null;
    const high = Math.max(bar.high, held?.high ?? -Infinity, last);
    const low = Math.min(bar.low, held?.low ?? Infinity, last);
    forming.current = { time, high, low };
    series.update({ time, open: bar.open, high, low, close: last });
  }, [last, candles, bars, seriesId, dp, height, overlay, oscillators, theme]);

  const fit = () => {
    const made = chart.current;
    if (made === null) return;
    made.timeScale().fitContent();
    made.panes().forEach((_, i) => made.priceScale("right", i).applyOptions({ autoScale: true }));
  };

  // Hovered, or the newest bar when the cursor is elsewhere, as every
  // charting package does: the reading is never empty.
  const at = hovered !== null && hovered >= 0 && hovered < bars.orig.length
    ? hovered
    : bars.orig.length - 1;
  const index = bars.orig[at] ?? -1;
  const onBar = candles[index] as Candle | undefined;

  // The strike nearest the cursor, while it is over the price pane.
  const strikeRow = (() => {
    const rows = overlay?.profile;
    if (!rows?.length || cursorPrice === null) return null;
    return rows.reduce((best, r) =>
      Math.abs(r.strike - cursorPrice) < Math.abs(best.strike - cursorPrice) ? r : best,
    );
  })();

  return (
    <div className="candlewrap">
      {!onBar && <p className="empty">No candles yet.</p>}
      {onBar && (
      <div className="reading">
        <span className="when">{when(onBar.at)}</span>
        <Reading label="O" value={onBar.open} dp={dp} />
        <Reading label="H" value={onBar.high} dp={dp} />
        <Reading label="L" value={onBar.low} dp={dp} />
        <Reading label="C" value={onBar.close} dp={dp} tone={onBar.close >= onBar.open ? "rise" : "fall"} />
        {(overlay?.lines ?? []).map((line, n) => {
          const value = line.values[index];
          return typeof value === "number" ? (
            <span key={line.label} className={`ind i${n % 5}`}>
              {line.label} {value.toFixed(dp)}
            </span>
          ) : null;
        })}
        {oscillators.map(({ line, colour }) => {
          const value = line.values[index];
          return typeof value === "number" ? (
            <span key={line.label} className={`ind i${colour % 5}`}>
              {line.label} {value.toFixed(BOUNDED[line.name] ? 0 : 2)}
            </span>
          ) : null;
        })}
        {oiPane && oiBars[at] && (
          <span className="oiread">
            <em>Fut OI</em> {compact(oiBars[at].oi)}
            {oiBars[at].change !== null && <> <i>{change(oiBars[at].change ?? 0)}</i></>}
            {oiBars[at].kind && <> · {BUILDUP_LABEL[oiBars[at].kind]}</>}
            {oiBars[at].roll && <> · expiry: the near month settled</>}
          </span>
        )}
        {strikeRow && (
          <span className="oiread">
            <em>{int(strikeRow.strike)}</em> PE {compact(strikeRow.put)}{" "}
            <i>{change(strikeRow.put - strikeRow.putPrev)}</i> · CE {compact(strikeRow.call)}{" "}
            <i>{change(strikeRow.call - strikeRow.callPrev)}</i>
          </span>
        )}
        <span className="sp" />
        <button className="xbtn" onClick={fit} title="Show every bar">
          Fit
        </button>
      </div>
      )}
      <div
        ref={box}
        className="lwchart"
        // A definite height, which a flex parent may then grow. Without one the
        // chart sizes itself to its own canvas and keeps growing.
        style={{ height: height + PANE_H * (oscillators.length + (oiPane ? 1 : 0)) }}
      />
    </div>
  );
}

/** Everything on the chart, from the shading at the back to the marks in front. */
function draw(
  made: IChartApi,
  p: Palette,
  candles: readonly Candle[],
  bars: { ms: number[]; orig: number[]; times: UTCTimestamp[] },
  dp: number,
  overlay: Overlay | undefined,
  oscillators: readonly { line: IndicatorLine; colour: number }[],
  price: { current: ISeriesApi<"Candlestick"> | null },
  oiBars: readonly (OiAtBar | null)[],
) {
  const { ms, orig, times } = bars;
  const first = ms[0];
  const lastMs = ms[ms.length - 1];
  const tone: Record<Kind, string> = {
    entry: p.you,
    exit: p.dim,
    stop: p.dn,
    target: p.up,
    rise: p.rise,
    fall: p.fall,
    wall: p.mkt,
  };

  /** The bar a moment falls in, by index: the last that opened at or before
      it. A fill is at a bar's open, so its mark belongs on that bar. */
  const barOf = (iso: string): number | null => {
    const want = Date.parse(iso);
    if (Number.isNaN(want) || want < first) return null;
    let lo = 0;
    let hi = ms.length - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (ms[mid] <= want) lo = mid;
      else hi = mid - 1;
    }
    return lo;
  };

  /** A span's first and last bar, or null when it falls outside the series. A
      span with one end off the series is clamped to it: it still began
      somewhere to the left. */
  const spanOf = (from: string, to: string): [number, number] | null => {
    const a = Date.parse(from);
    const b = Date.parse(to);
    if (Number.isNaN(a) || Number.isNaN(b) || b < first || a > lastMs) return null;
    return [barOf(from) ?? 0, barOf(to) ?? ms.length - 1];
  };

  const blank = (i: number): WhitespaceData<Time> => ({ time: times[i] });

  // The holding period, shaded full height behind everything else: an area
  // pinned to the top of its own hidden scale fills down to the floor.
  if (overlay?.band) {
    const span = spanOf(overlay.band.from, overlay.band.to);
    if (span !== null) {
      const held = made.addSeries(AreaSeries, {
        priceScaleId: "held",
        lineColor: "transparent",
        topColor: p.held,
        bottomColor: p.held,
        lastValueVisible: false,
        priceLineVisible: false,
        crosshairMarkerVisible: false,
        autoscaleInfoProvider: () => ({ priceRange: { minValue: 0, maxValue: 1 } }),
      });
      held.priceScale().applyOptions({ scaleMargins: { top: 0, bottom: 0 }, visible: false });
      held.setData(times.map((t, i) => (i >= span[0] && i <= span[1] ? { time: t, value: 1 } : blank(i))));
    }
  }

  // Volume along the floor of the price pane, where there is any. An index
  // trades none, and a row of zero bars is not information.
  if (orig.some((i) => candles[i].volume > 0)) {
    const volume = made.addSeries(HistogramSeries, {
      priceScaleId: "volume",
      priceFormat: { type: "volume" },
      lastValueVisible: false,
      priceLineVisible: false,
    });
    volume.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 }, visible: false });
    volume.setData(
      orig.map((i, n) => {
        const c = candles[i];
        return { time: times[n], value: c.volume, color: fade(c.close >= c.open ? p.rise : p.fall, 0.3) };
      }),
    );
  }

  const step = 1 / 10 ** dp;
  const series = made.addSeries(CandlestickSeries, {
    upColor: p.rise,
    downColor: p.fall,
    borderUpColor: p.rise,
    borderDownColor: p.fall,
    wickUpColor: p.rise,
    wickDownColor: p.fall,
    priceFormat: { type: "price", precision: dp, minMove: step },
  });
  series.setData(
    orig.map((i, n) => {
      const c = candles[i];
      return { time: times[n], open: c.open, high: c.high, low: c.low, close: c.close };
    }),
  );
  price.current = series;

  if (overlay?.profile?.length) {
    series.attachPrimitive(new OiProfile(overlay.profile, { ink: p.fg, wall: p.mkt }));
  }

  for (const level of overlay?.levels ?? []) {
    series.createPriceLine({
      price: level.price,
      color: tone[level.kind],
      lineWidth: 1,
      lineStyle: LineStyle.Dashed,
      axisLabelVisible: true,
      title: level.label,
    });
  }

  // Indicator lines over the candles. Slowest first, which
  // `StrategySpec.indicators` already orders them by.
  (overlay?.lines ?? []).forEach((line, n) => {
    const drawn = made.addSeries(LineSeries, {
      color: p.ind[n % 5],
      lineWidth: 2,
      priceFormat: { type: "price", precision: dp, minMove: step },
      lastValueVisible: false,
      priceLineVisible: false,
      crosshairMarkerVisible: false,
    });
    drawn.setData(
      orig.map((i, k) => {
        const value = line.values[i];
        // A gap, not a jump to zero: the indicator had no value here.
        return typeof value === "number" ? { time: times[k], value } : blank(k);
      }),
    );
  });

  // A level that only existed between two moments, labelled where it ended.
  // Off-screen ones are dropped rather than clamped into a line across the chart.
  for (const seg of overlay?.segments ?? []) {
    const span = spanOf(seg.from, seg.to);
    if (span === null) continue;
    const drawn = made.addSeries(LineSeries, {
      color: tone[seg.kind],
      lineWidth: 1,
      lineStyle: LineStyle.Dotted,
      priceFormat: { type: "price", precision: dp, minMove: step },
      lastValueVisible: false,
      priceLineVisible: false,
      crosshairMarkerVisible: false,
    });
    const ends = span[0] === span[1] ? [span[0]] : span;
    drawn.setData(ends.map((i) => ({ time: times[i], value: seg.price })));
    createSeriesMarkers(drawn, [
      {
        time: times[span[1]],
        position: "atPriceTop",
        price: seg.price,
        shape: "square",
        size: 0,
        color: tone[seg.kind],
        text: seg.label,
      },
    ]);
  }

  // The two moments that matter most on a trade chart, at the price they filled.
  const marks: SeriesMarker<Time>[] = [];
  for (const mark of overlay?.marks ?? []) {
    const i = barOf(mark.at);
    if (i === null) continue;
    const buying = (mark.kind === "entry") === (mark.side === "long");
    marks.push({
      time: times[i],
      position: buying ? "atPriceBottom" : "atPriceTop",
      price: mark.price,
      shape: buying ? "arrowUp" : "arrowDown",
      color: mark.kind === "entry" ? p.you : p.dim,
      text: mark.kind === "entry" ? (mark.side === "long" ? "buy" : "sell") : "close",
    });
  }
  marks.sort((a, b) => (a.time as number) - (b.time as number));
  if (marks.length) createSeriesMarkers(series, marks);

  // Oscillators, a pane each: an RSI and Bitcoin share an axis no better than
  // two oscillators with different ranges do.
  oscillators.forEach(({ line, colour }, n) => {
    const bounds = BOUNDED[line.name];
    const drawn = made.addSeries(
      LineSeries,
      {
        color: p.ind[colour % 5],
        lineWidth: 2,
        title: line.label,
        priceLineVisible: false,
        priceFormat: { type: "price", precision: bounds ? 0 : 2, minMove: bounds ? 1 : 0.01 },
        autoscaleInfoProvider: bounds
          ? () => ({ priceRange: { minValue: bounds[0], maxValue: bounds[1] } })
          : undefined,
      },
      n + 1,
    );
    drawn.priceScale().applyOptions({ scaleMargins: { top: 0.08, bottom: 0.08 } });
    drawn.setData(
      orig.map((i, k) => {
        const value = line.values[i];
        return typeof value === "number" ? { time: times[k], value } : blank(k);
      }),
    );
    for (const guide of GUIDES[line.name] ?? []) {
      drawn.createPriceLine({
        price: guide,
        color: p.line,
        lineWidth: 1,
        lineStyle: LineStyle.Dashed,
        axisLabelVisible: false,
      });
    }
  });

  // Futures OI, a pane of its own under the oscillators: the change each bar,
  // coloured by what price did with it, over the level as a thin line. New
  // positions (OI up) are solid and closing ones faded; up moves take the
  // candles' rising colour and down moves their falling one - so a solid
  // purple bar is longs being built and a faded grey one longs letting go.
  if (oiBars.some((b) => b !== null)) {
    const pane = oscillators.length + 1;
    const tone = {
      "long-buildup": p.rise,
      "short-covering": fade(p.rise, 0.45),
      "short-buildup": p.fall,
      "long-unwinding": fade(p.fall, 0.45),
    } as const;
    const level = made.addSeries(
      LineSeries,
      {
        color: fade(p.dim, 0.7),
        lineWidth: 1,
        priceScaleId: "oi-level",
        priceFormat: { type: "volume" },
        lastValueVisible: false,
        priceLineVisible: false,
        crosshairMarkerVisible: false,
      },
      pane,
    );
    level.priceScale().applyOptions({ visible: false, scaleMargins: { top: 0.1, bottom: 0.1 } });
    level.setData(oiBars.map((b, k) => (b ? { time: times[k], value: b.oi } : blank(k))));
    const changes = made.addSeries(
      HistogramSeries,
      {
        title: "Fut OI Δ",
        priceFormat: { type: "volume" },
        priceLineVisible: false,
        lastValueVisible: false,
      },
      pane,
    );
    changes.priceScale().applyOptions({ scaleMargins: { top: 0.1, bottom: 0.1 } });
    changes.setData(
      oiBars.map((b, k) =>
        b && b.change !== null
          ? { time: times[k], value: b.change, color: b.kind ? tone[b.kind] : fade(p.dim, 0.4) }
          : b?.roll
            ? { time: times[k], value: 0, color: "transparent" }
            : blank(k),
      ),
    );
    // Where the near month expired: what was left in it settled and is gone
    // from the total, so the bar is marked rather than drawn as a buildup.
    const rolls: SeriesMarker<Time>[] = [];
    oiBars.forEach((b, k) => {
      if (b?.roll) {
        rolls.push({ time: times[k], position: "aboveBar", shape: "circle", size: 0, color: p.dim, text: "expiry" });
      }
    });
    if (rolls.length) createSeriesMarkers(changes, rolls);
  }
}
