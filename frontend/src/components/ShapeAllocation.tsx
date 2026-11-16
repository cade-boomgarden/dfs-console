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


/** Showdown team splits, counted from the captain's side (captain included):
 *  "4-2" = captain + 3 teammates, 2 from the other team. */
export const SPLITS = ["5-1", "4-2", "3-3", "2-4", "1-5"] as const;
const SPLIT_HINT: Record<string, string> = {
  "5-1": "captain + 4 teammates, 1 opponent",
  "4-2": "captain + 3 teammates, 2 opponents",
  "3-3": "captain + 2 teammates, 3 opponents",
  "2-4": "captain + 1 teammate, 4 opponents",
  "1-5": "captain alone, 5 opponents",
};
/** Even mix: the model's own basis (expected payout or tail mass) then
 *  ranks which side carries each split. */
export const SPLIT_DEFAULT: Record<string, number> = Object.fromEntries(SPLITS.map((k) => [k, 20]));

export function SplitAllocation({ value, onChange }: {
  value: Record<string, number>;
  onChange: (v: Record<string, number>) => void;
}) {
  const total = SPLITS.reduce((a, k) => a + (value[k] || 0), 0);
  const pct = (k: string) => (total > 0 ? ((value[k] || 0) / total) * 100 : 0);
  return (
    <div className="space-y-2">
      <div className="flex items-baseline gap-3 flex-wrap">
        <span className="eyebrow">Team split allocation</span>
        <span className="text-[10px] text-[var(--dim)]">
          players from the captain's team – the other team, captain included · relative weights,
          normalised to the target mix · 0 excludes a split
        </span>
        <span className="ml-auto flex gap-3">
          <button type="button" className="text-[10px] text-[var(--dim)] hover:text-[var(--ink)]"
            onClick={() => onChange({ ...SPLIT_DEFAULT })}>even mix</button>
          <button type="button" className="text-[10px] text-[var(--dim)] hover:text-[var(--ink)]"
            onClick={() => onChange(Object.fromEntries(SPLITS.map((k) => [k, 0])))}>clear</button>
        </span>
      </div>
      <div className="grid grid-cols-5 gap-2">
        {SPLITS.map((k) => {
          const p = pct(k);
          return (
            <div key={k} title={SPLIT_HINT[k]}
              className={`rounded border px-2 py-1 ${p > 0 ? "border-[var(--line)]" : "border-transparent opacity-40"}`}>
              <div className="flex items-center gap-2">
                <span className="num text-xs w-8">{k}</span>
                <input type="number" min={0} step={1} value={value[k] ?? 0}
                  onChange={(e) => onChange({ ...value, [k]: Math.max(0, Number(e.target.value)) })}
                  className="w-14 px-1 py-0.5 text-right" />
                <span className="num text-[11px] w-10 text-right">{num(p, 0)}%</span>
              </div>
              <div className="text-[9px] text-[var(--dim)] mt-0.5 truncate">{SPLIT_HINT[k]}</div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
