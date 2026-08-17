import { num } from "../ui";

/** Stack shape allocation (requirements 6a/6b): the operator picks the mix of
 *  shapes, the model picks which games carry them. Weights are relative and
 *  normalised, so they read as target percentages. A shape at 0 never appears. */
export const SHAPES: { key: string; label: string; teammates: number; bringback: number }[] = [];
for (let t = 0; t <= 3; t++) {
  for (let b = 0; b <= 2; b++) {
    const base = ["NAKED", "SINGLE", "DOUBLE", "ONSLAUGHT"][t];
    SHAPES.push({
      key: `${t}-${b}`,
      label: b >= 2 ? `GAME_${base}` : b === 1 ? `${base}_W_BB` : base,
      teammates: t, bringback: b,
    });
  }
}

/** The model's own default mix, so "reset" is meaningful. */
export const MODEL_DEFAULT: Record<string, number> = Object.fromEntries(
  SHAPES.map((s) => [s.key, Math.round((1 + 0.35 * s.teammates) * 10)]),
);

export function ShapeAllocation({ value, onChange }: {
  value: Record<string, number>;
  onChange: (v: Record<string, number>) => void;
}) {
  const total = Object.values(value).reduce((a, b) => a + (b || 0), 0);
  const pct = (k: string) => (total > 0 ? ((value[k] || 0) / total) * 100 : 0);
  const set = (k: string, v: number) => onChange({ ...value, [k]: Math.max(0, v) });

  return (
    <div className="space-y-2">
      <div className="flex items-baseline gap-3 flex-wrap">
        <span className="eyebrow">Stack shape allocation</span>
        <span className="text-[10px] text-[var(--dim)]">
          relative weights, normalised to the target mix — 0 excludes a shape entirely
        </span>
        <span className="ml-auto flex gap-3">
          <button type="button" className="text-[10px] text-[var(--dim)] hover:text-[var(--ink)]"
            onClick={() => onChange({ ...MODEL_DEFAULT })}>reset to model</button>
          <button type="button" className="text-[10px] text-[var(--dim)] hover:text-[var(--ink)]"
            onClick={() => onChange(Object.fromEntries(SHAPES.map((s) => [s.key, 0])))}>clear</button>
        </span>
      </div>
      <table className="w-full">
        <thead>
          <tr>
            <th className="text-left text-[10px] uppercase tracking-wider text-[var(--dim)] px-1 py-1">
              QB teammates
            </th>
            {["no bringback", "1 bringback", "2 bringback"].map((h) => (
              <th key={h} className="text-[10px] uppercase tracking-wider text-[var(--dim)] px-1 py-1">{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {[0, 1, 2, 3].map((t) => (
            <tr key={t}>
              <td className="px-1 py-1 text-[11px] text-[var(--dim)] whitespace-nowrap">
                {["+0 (naked)", "+1", "+2", "+3"][t]}
              </td>
              {[0, 1, 2].map((b) => {
                const k = `${t}-${b}`;
                const label = SHAPES.find((s) => s.key === k)!.label;
                const p = pct(k);
                return (
                  <td key={b} className="px-1 py-1">
                    <div className={`rounded border px-2 py-1 ${p > 0 ? "border-[var(--line)]" : "border-transparent opacity-40"}`}>
                      <div className="flex items-center gap-2">
                        <input type="number" min={0} step={1} value={value[k] ?? 0}
                          onChange={(e) => set(k, Number(e.target.value))}
                          className="w-14 px-1 py-0.5 text-right" />
                        <span className="num text-[11px] w-10 text-right">{num(p, 0)}%</span>
                      </div>
                      <div className="text-[9px] text-[var(--dim)] mt-0.5 truncate">{label}</div>
                    </div>
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
