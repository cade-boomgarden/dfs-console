import { ReactNode } from "react";

export function Btn({ children, onClick, kind = "default", disabled, type = "button" }: {
  children: ReactNode; onClick?: () => void; kind?: "default" | "primary" | "danger" | "ghost";
  disabled?: boolean; type?: "button" | "submit";
}) {
  const styles: Record<string, string> = {
    default: "bg-[var(--raised)] border border-[var(--line)] hover:border-[var(--dim)]",
    primary: "bg-[var(--amber)] text-black font-semibold hover:brightness-110",
    danger: "bg-transparent border border-[var(--down)] text-[var(--down)] hover:bg-[var(--down)] hover:text-black",
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

/** The signature element: an inline sims-distribution strip with
 *  floor / median / ceiling ticks. Rendered anywhere a lineup appears. */
export function DistStrip({ histogram, edges, floor, median, ceiling, width = 160, height = 26 }: {
  histogram?: number[]; edges?: number[]; floor?: number; median?: number; ceiling?: number;
  width?: number; height?: number;
}) {
  if (!histogram || !histogram.length || !edges) return <span className="text-[var(--dim)]">—</span>;
  const max = Math.max(...histogram, 1);
  const lo = edges[0], hi = edges[edges.length - 1];
  const x = (v: number) => ((v - lo) / (hi - lo || 1)) * width;
  const bw = width / histogram.length;
  return (
    <svg width={width} height={height} className="block">
      {histogram.map((c, i) => (
        <rect key={i} x={i * bw} width={Math.max(bw - 0.5, 0.5)}
          y={height - (c / max) * (height - 4)} height={(c / max) * (height - 4)}
          fill="var(--chart)" />
      ))}
      {floor !== undefined && <line x1={x(floor)} x2={x(floor)} y1={0} y2={height} stroke="var(--down)" strokeWidth={1} />}
      {median !== undefined && <line x1={x(median)} x2={x(median)} y1={0} y2={height} stroke="var(--ink)" strokeWidth={1} />}
      {ceiling !== undefined && <line x1={x(ceiling)} x2={x(ceiling)} y1={0} y2={height} stroke="var(--up)" strokeWidth={1} />}
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
