import { type ReactNode, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { niceTicks } from "../analytics";

export interface ChartSeries {
  name: string;
  /** A CSS colour, e.g. "var(--series-1)". */
  color: string;
}

/** One x position: a value per series (null = no value), its tooltip, and where a click goes. */
export interface ChartColumn {
  key: string;
  /** 0..1 along the x axis. */
  x: number;
  values: (number | null)[];
  tooltip: ReactNode;
  href?: string;
}

interface Props {
  label: string;
  series: ChartSeries[];
  columns: ChartColumn[];
  /** How many x positions the axis has room for (sizes the hover targets). */
  slots: number;
  xTicks: { x: number; label: string }[];
  yFormat: (v: number) => string;
  /** Fixed y domain; otherwise the data's range, from zero when zeroBased. */
  yDomain?: [number, number];
  zeroBased?: boolean;
  /** Draw the first series as bars from the baseline instead of dots. */
  bars?: boolean;
  /** Join each column's values with a thin vertical line (e.g. median to p90). */
  range?: boolean;
  height?: number;
}

const M = { top: 10, right: 12, bottom: 22, left: 64 };

/**
 * Dots (or bars) at the recorded points only. Nothing joins one x position
 * to the next: a line between two points would claim values nobody measured.
 */
export function PointChart(props: Props) {
  const { series, columns, slots, xTicks, yFormat, height = 180 } = props;
  const wrap = useRef<HTMLDivElement>(null);
  const width = useWidth(wrap);
  const [hover, setHover] = useState<number | null>(null);
  const navigate = useNavigate();

  const values = columns.flatMap((c) => c.values.filter((v): v is number => v != null));
  const lo = props.zeroBased || props.bars ? 0 : Math.min(...values);
  const ticks = props.yDomain ? niceTicks(...props.yDomain) : niceTicks(lo, Math.max(...values));
  const [y0, y1] = props.yDomain ?? [ticks[0] ?? 0, ticks[ticks.length - 1] ?? 1];

  const plotW = Math.max(0, width - M.left - M.right);
  const plotH = height - M.top - M.bottom;
  const slotW = plotW / Math.max(1, slots);
  const barW = Math.max(2, Math.min(24, slotW * 0.6));
  // Inset so the first and last marks stay clear of the axis labels.
  const inset = props.bars ? barW / 2 + 4 : 8;
  const px = (x: number) => M.left + inset + x * Math.max(0, plotW - 2 * inset);
  const py = (v: number) => M.top + plotH - ((v - y0) / (y1 - y0 || 1)) * plotH;

  const hovered = hover != null ? columns[hover] : undefined;
  const open = (c: ChartColumn) => c.href && navigate(c.href);

  return (
    <div className="chart" ref={wrap}>
      {series.length > 1 && (
        <div className="chart__legend">
          {series.map((s) => (
            <span key={s.name} className="chart__key">
              <span className="chart__swatch" style={{ background: s.color }} aria-hidden />
              {s.name}
            </span>
          ))}
        </div>
      )}
      {width > 0 && (
        <svg width={width} height={height} role="img" aria-label={props.label} className="chart__svg">
          {ticks
            .filter((t) => t >= y0 && t <= y1)
            .map((t) => (
              <g key={t}>
                <line className="chart__grid" x1={M.left} x2={M.left + plotW} y1={py(t)} y2={py(t)} />
                <text className="chart__tick" x={M.left - 6} y={py(t)} dy="0.32em" textAnchor="end">
                  {yFormat(t)}
                </text>
              </g>
            ))}
          {xTicks.map((t) => (
            <text
              key={t.label}
              className="chart__tick"
              x={px(t.x)}
              y={height - 6}
              textAnchor={t.x <= 0 ? "start" : t.x >= 1 ? "end" : "middle"}
            >
              {t.label}
            </text>
          ))}
          {hovered && <line className="chart__cross" x1={px(hovered.x)} x2={px(hovered.x)} y1={M.top} y2={M.top + plotH} />}

          {columns.map((c) => {
            const x = px(c.x);
            const present = c.values.filter((v): v is number => v != null);
            return (
              <g key={c.key}>
                {props.range && present.length > 1 && (
                  <line
                    className="chart__range"
                    x1={x}
                    x2={x}
                    y1={py(Math.min(...present))}
                    y2={py(Math.max(...present))}
                  />
                )}
                {c.values.map((v, i) => {
                  if (v == null) return null;
                  if (props.bars && i === 0) {
                    return <path key={i} d={barPath(x - barW / 2, barW, py(v), py(y0))} fill={series[0]!.color} />;
                  }
                  return <circle key={i} className="chart__dot" cx={x} cy={py(v)} r={4} fill={series[i]!.color} />;
                })}
              </g>
            );
          })}

          {/* Hover and focus targets, wider than the marks. */}
          {columns.map((c, i) => (
            <rect
              key={c.key}
              className={`chart__hit${c.href ? " chart__hit--link" : ""}`}
              x={px(c.x) - Math.max(slotW, 8) / 2}
              width={Math.max(slotW, 8)}
              y={M.top}
              height={plotH}
              tabIndex={0}
              role={c.href ? "link" : undefined}
              aria-label={c.key}
              onMouseEnter={() => setHover(i)}
              onMouseLeave={() => setHover(null)}
              onFocus={() => setHover(i)}
              onBlur={() => setHover(null)}
              onClick={() => open(c)}
              onKeyDown={(e) => e.key === "Enter" && open(c)}
            />
          ))}
        </svg>
      )}
      {hovered && (
        <div
          className="chart__tip"
          style={{ left: Math.min(Math.max(px(hovered.x), 90), width - 90), top: M.top }}
          role="status"
        >
          {hovered.tooltip}
        </div>
      )}
    </div>
  );
}

/** A bar with 2px rounded top corners, standing on the baseline. */
function barPath(x: number, w: number, top: number, base: number): string {
  const r = Math.min(2, w / 2, Math.max(0, base - top));
  return `M${x},${base} V${top + r} Q${x},${top} ${x + r},${top} H${x + w - r} Q${x + w},${top} ${x + w},${top + r} V${base} Z`;
}

function useWidth(ref: React.RefObject<HTMLElement | null>): number {
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const observer = new ResizeObserver(([entry]) => setWidth(Math.floor(entry!.contentRect.width)));
    observer.observe(el);
    return () => observer.disconnect();
  }, [ref]);
  return width;
}
