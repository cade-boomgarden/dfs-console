import { ReactNode } from "react";

export function Btn({ children, onClick, kind = "default", disabled, type = "button" }: {
  children: ReactNode; onClick?: () => void; kind?: "default" | "primary" | "danger" | "ghost";
  disabled?: boolean; type?: "button" | "submit";
}) {
  const styles: Record<string, string> = {
    default: "bg-[var(--raised)] border border-[var(--line)] hover:border-[var(--dim)]",
    primary: "bg-[var(--accent)] text-black font-bold hover:bg-[var(--ink)]",
    danger: "bg-transparent border border-[var(--dim)] text-[var(--dim)] hover:border-[var(--ink)] hover:text-[var(--ink)]",
    ghost: "bg-transparent text-[var(--dim)] hover:text-[var(--ink)]",
  };
  return (
    <button type={type} disabled={disabled} onClick={onClick}
      className={`px-3 py-1.5 rounded text-xs disabled:opacity-40 disabled:cursor-not-allowed ${styles[kind]}`}>
      {children}
    </button>
  );
}

export function Badge({ children, tone = "dim" }: { children: ReactNode; tone?: "dim" | "up" | "down" | "amber" }) {
  const c = { dim: "var(--dim)", up: "var(--up)", down: "var(--down)", amber: "var(--amber)" }[tone];
  return (
    <span className="px-1.5 py-0.5 rounded text-[10px] uppercase tracking-wider border"
      style={{ color: c, borderColor: c }}>
      {children}
    </span>
  );
}

/** The signature element: an inline sims-distribution strip.
 *
 *  Every lineup's histogram is binned over ITS OWN range, so drawing each on
 *  its own axis renders forty identical bell curves. Pass a shared `domain`
 *  (and `yMax`) across a set and the strips become comparable: position shows
 *  the mean, width shows the spread. Heights are densities, not raw counts,
 *  because bin widths differ between lineups. */
export function distDomain(
  rows: { evaluation: { hist_edges?: number[] } }[],
): [number, number] | undefined {
  const lo = Math.min(...rows.map((r) => r.evaluation.hist_edges?.[0] ?? Infinity));
  const hi = Math.max(...rows.map((r) => {
    const e = r.evaluation.hist_edges;
    return e ? e[e.length - 1] : -Infinity;
  }));
  return Number.isFinite(lo) && Number.isFinite(hi) ? [lo, hi] : undefined;
}

export function distMaxDensity(
  rows: { evaluation: { histogram?: number[]; hist_edges?: number[] } }[],
): number {
  let max = 0;
  for (const r of rows) {
    const h = r.evaluation.histogram, e = r.evaluation.hist_edges;
    if (!h?.length || !e?.length) continue;
    const bw = (e[e.length - 1] - e[0]) / h.length || 1;
    for (const c of h) max = Math.max(max, c / bw);
  }
  return max || 1;
}

export function DistStrip({ histogram, edges, floor, median, ceiling, domain, yMax,
                            width = 160, height = 26 }: {
  histogram?: number[]; edges?: number[]; floor?: number; median?: number; ceiling?: number;
  domain?: [number, number]; yMax?: number; width?: number; height?: number;
}) {
  if (!histogram || !histogram.length || !edges) return <span className="text-[var(--dim)]">—</span>;
  const [lo, hi] = domain ?? [edges[0], edges[edges.length - 1]];
  const span = hi - lo || 1;
  const x = (v: number) => ((v - lo) / span) * width;
  const binW = (edges[edges.length - 1] - edges[0]) / histogram.length || 1;
  const max = yMax ?? Math.max(...histogram.map((c) => c / binW), 1);
  const pxW = (binW / span) * width;
  return (
    <svg width={width} height={height} className="block">
      {histogram.map((c, i) => {
        const h = ((c / binW) / max) * (height - 4);
        return (
          <rect key={i} x={x(edges[0] + i * binW)} width={Math.max(pxW - 0.3, 0.4)}
            y={height - h} height={h} fill="var(--chart)" />
        );
      })}
      {/* floor: dashed + dim | median: solid + brightest | ceiling: dashed + bright.
          Dash pattern carries the distinction so nothing depends on hue. */}
      {floor !== undefined && (
        <line x1={x(floor)} x2={x(floor)} y1={0} y2={height}
              stroke="var(--mute)" strokeWidth={1} strokeDasharray="2 2" />)}
      {ceiling !== undefined && (
        <line x1={x(ceiling)} x2={x(ceiling)} y1={0} y2={height}
              stroke="var(--ink)" strokeWidth={1} strokeDasharray="2 2" />)}
      {median !== undefined && (
        <line x1={x(median)} x2={x(median)} y1={0} y2={height}
              stroke="var(--accent)" strokeWidth={1.5} />)}
    </svg>
  );
}

export function Progress({ value, message }: { value: number; message?: string }) {
  return (
    <div className="space-y-1">
      <div className="h-1.5 bg-[var(--raised)] rounded overflow-hidden">
        <div className="h-full bg-[var(--amber)] transition-all" style={{ width: `${Math.round(value * 100)}%` }} />
      </div>
      {message && <div className="text-[11px] text-[var(--dim)]">{message}</div>}
    </div>
  );
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="eyebrow">{label}</span>
      {children}
    </label>
  );
}

export function num(v: number | null | undefined, d = 1): string {
  return v === null || v === undefined ? "—" : v.toFixed(d);
}
export function money(v: number | null | undefined): string {
  return v === null || v === undefined ? "—" : `$${v.toLocaleString()}`;
}
