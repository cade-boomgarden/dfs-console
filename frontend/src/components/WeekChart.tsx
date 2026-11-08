import { ReactNode, useRef, useState } from "react";

/** One series on a shared week axis. `line` draws a 2px line with 8px dots;
 *  `bar` draws thin bars from zero; `tick` draws a short horizontal mark per
 *  week (a reference value such as the projection), so series differ by
 *  shape, never by color alone. */
export interface WeekSeries {
  key: string; label: string; kind: "line" | "bar" | "tick";
  values: Map<number, number | null | undefined>;
}

/** Small-multiple chart over NFL weeks. One y-axis per chart, by rule:
 *  salary and points are separate charts on the same x-axis, never a dual
 *  axis. Hovering anywhere in a week's column shows a crosshair and a tooltip
 *  with every series at that week plus `extra(week)`. */
/** Smallest "nice" number >= v (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8 x 10^k). */
function niceCeil(v: number): number {
  if (v <= 0) return 0;
  const e = 10 ** Math.floor(Math.log10(v));
  for (const m of [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) if (m * e >= v - 1e-12) return m * e;
  return 10 * e;
}

export function WeekChart({ weeks, series, format, height = 96, yZero = true,
                            yDomain, yCap, yMinMax, extra, title, legend }: {
  weeks: number[]; series: WeekSeries[]; format: (v: number) => string;
  height?: number; yZero?: boolean; yDomain?: [number, number];
  /** zero-based charts: never extend the axis past this (1 for shares) */
  yCap?: number;
  /** zero-based charts: axis top is at least this (keeps all-zero series flat at the bottom) */
  yMinMax?: number;
  extra?: (week: number) => ReactNode; title?: ReactNode; legend?: boolean;
}) {
  const W = 640, padL = 44, padR = 8, padT = 6, padB = 18;
  const ref = useRef<SVGSVGElement>(null);
  const [hover, setHover] = useState<{ week: number; x: number } | null>(null);

  const vals = series.flatMap((s) => weeks.map((w) => s.values.get(w))).filter(
    (v): v is number => v !== null && v !== undefined && Number.isFinite(v));
  let [lo, hi] = yDomain ?? [Math.min(...vals), Math.max(...vals)];
  if (!yDomain) {
    if (!vals.length) { lo = 0; hi = 1; }
    if (yZero) {
      lo = Math.min(0, lo);
      hi = niceCeil(Math.max(hi * 1.05, yMinMax ?? 0)) || 1;
      if (yCap !== undefined) hi = Math.min(hi, Math.max(yCap, Math.max(...vals, 0)));
    } else {
      const pad = (hi - lo) * 0.08 || Math.abs(hi) * 0.1 || 1;
      hi += pad; lo -= pad;
    }
  }
  const n = weeks.length || 1;
  const band = (W - padL - padR) / n;
  const x = (i: number) => padL + band * (i + 0.5);
  const y = (v: number) => padT + (1 - (v - lo) / (hi - lo || 1)) * (height - padT - padB);
  const ticks = [lo, (lo + hi) / 2, hi];

  const onMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const r = ref.current!.getBoundingClientRect();
    const px = ((e.clientX - r.left) / r.width) * W;
    const i = Math.floor((px - padL) / band);
    if (i < 0 || i >= n) { setHover(null); return; }
    setHover({ week: weeks[i], x: ((x(i)) / W) * r.width });
  };

  const bars = series.filter((s) => s.kind === "bar");
  const barW = Math.max(Math.min(band * 0.5, 18) / Math.max(bars.length, 1), 2);

  return (
    <div>
      {(title || (legend && series.length > 1)) && (
        <div className="flex items-baseline gap-3 mb-1">
          {title && <span className="eyebrow">{title}</span>}
          {legend && series.length > 1 && (
            <span className="ml-auto flex gap-3 text-[10px] text-[var(--dim)]">
              {series.map((s) => (
                <span key={s.key} className="flex items-center gap-1">
                  <svg width={16} height={8}>
                    {s.kind === "bar"
                      ? <rect x={4} y={0} width={8} height={8} rx={1} fill="var(--chart)" />
                      : s.kind === "tick"
                        ? <line x1={3} x2={13} y1={4} y2={4} stroke="var(--ink)" strokeWidth={2} />
                        : <><line x1={0} x2={16} y1={4} y2={4} stroke="var(--ink)" strokeWidth={2} />
                            <circle cx={8} cy={4} r={3} fill="var(--ink)" /></>}
                  </svg>
                  {s.label}
                </span>
              ))}
            </span>
          )}
        </div>
      )}
      <div className="relative">
      <svg ref={ref} viewBox={`0 0 ${W} ${height}`} className="w-full block" style={{ height }}
        preserveAspectRatio="none" onMouseMove={onMove} onMouseLeave={() => setHover(null)}>
        {ticks.map((t, i) => (
          <g key={i}>
            <line x1={padL} x2={W - padR} y1={y(t)} y2={y(t)} stroke="var(--line)" strokeWidth={1}
              vectorEffect="non-scaling-stroke" />
          </g>
        ))}
        {hover && (
          <rect x={x(weeks.indexOf(hover.week)) - band / 2} width={band} y={padT}
            height={height - padT - padB} fill="var(--raised)" />
        )}
        {bars.map((s, bi) => weeks.map((w, i) => {
          const v = s.values.get(w);
          if (v === null || v === undefined) return null;
          const top = y(Math.max(v, 0)), base = y(Math.max(lo, 0));
          const cx = x(i) + (bi - (bars.length - 1) / 2) * barW;
          return <rect key={`${s.key}-${w}`} x={cx - barW / 2 + 0.5} width={barW - 1}
            y={Math.min(top, base)} height={Math.max(Math.abs(base - top), 1)} rx={1.5}
            fill="var(--chart)" />;
        }))}
        {series.filter((s) => s.kind === "line").map((s) => {
          const pts = weeks.map((w, i) => [i, s.values.get(w)] as const)
            .filter((p): p is readonly [number, number] => p[1] !== null && p[1] !== undefined);
          // break the line across missing weeks rather than bridging them
          const segs: (readonly [number, number])[][] = [];
          pts.forEach((p, k) => {
            if (k === 0 || p[0] !== pts[k - 1][0] + 1) segs.push([]);
            segs[segs.length - 1].push(p);
          });
          return (
            <g key={s.key}>
              {segs.map((seg, k) => (
                <polyline key={k} fill="none" stroke="var(--ink)" strokeWidth={2}
                  vectorEffect="non-scaling-stroke"
                  points={seg.map(([i, v]) => `${x(i)},${y(v)}`).join(" ")} />
              ))}
            </g>
          );
        })}
        {series.filter((s) => s.kind === "tick").map((s) => weeks.map((w, i) => {
          const v = s.values.get(w);
          if (v === null || v === undefined) return null;
          const half = Math.max(Math.min(band * 0.5, 18), 6) / 2 + 3;
          return <line key={`${s.key}-${w}`} x1={x(i) - half} x2={x(i) + half} y1={y(v)} y2={y(v)}
            stroke="var(--ink)" strokeWidth={2} vectorEffect="non-scaling-stroke" />;
        }))}
      </svg>
      {/* dots and labels live in HTML so preserveAspectRatio=none cannot squash them */}
      <div className="absolute inset-0 pointer-events-none">
        {series.filter((s) => s.kind === "line").map((s) => weeks.map((w, i) => {
          const v = s.values.get(w);
          if (v === null || v === undefined) return null;
          return <span key={`${s.key}-${w}`} className="absolute w-2 h-2 -ml-1 -mt-1 rounded-full bg-[var(--ink)] ring-2 ring-[var(--panel)]"
            style={{ left: `${(x(i) / W) * 100}%`, top: y(v) }} />;
        }))}
        {ticks.map((t, i) => (
          <span key={i} className="absolute left-0 -mt-1.5 num text-[9px] text-[var(--mute)]"
            style={{ top: y(t) }}>{format(t)}</span>
        ))}
        {weeks.map((w, i) => (
          <span key={w} className="absolute bottom-0 -translate-x-1/2 num text-[9px] text-[var(--mute)]"
            style={{ left: `${(x(i) / W) * 100}%` }}>{w}</span>
        ))}
      </div>
      {hover && (
        <div className="absolute z-20 pointer-events-none panel shadow-xl shadow-black/50 px-2 py-1.5 text-[11px] num whitespace-nowrap"
          style={{ left: hover.x + 12, top: 0, transform: hover.x > 420 ? "translateX(calc(-100% - 24px))" : undefined }}>
          <div className="text-[var(--dim)] mb-0.5">Week {hover.week}</div>
          {series.map((s) => {
            const v = s.values.get(hover.week);
            return (
              <div key={s.key} className="flex justify-between gap-4">
                <span className="text-[var(--dim)] font-[family-name:var(--font-ui)]">{s.label}</span>
                <span>{v === null || v === undefined ? "—" : format(v)}</span>
              </div>
            );
          })}
          {extra?.(hover.week)}
        </div>
      )}
      </div>
    </div>
  );
}
