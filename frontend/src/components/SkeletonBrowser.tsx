import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, SkeletonNeff, SkeletonStat, SkeletonStatsResponse } from "../api";
import { Badge, num } from "../ui";

/** Skeleton browser + allocation (requirements 6b, item 17).
 *
 *  Browse the enumerated skeletons with per-skeleton stats from the resident
 *  sims matrix, put a per-game emphasis on top of the shape mix, exclude
 *  individual skeletons, and watch the STRUCTURAL N_eff of the allocation
 *  update live. Structural = lineups within one skeleton counted as fully
 *  correlated, so it is the floor the composition guarantees; real builds add
 *  filler diversity on top of it. */
export function SkeletonBrowser({
  slateId, enabled, nLineups, contestId, shapes, dstWithQb,
  gameWeights, onGameWeights, excluded, onExcluded,
}: {
  slateId: string;
  enabled: boolean;
  nLineups: number;
  contestId: number | null;
  shapes: Record<string, number>;
  dstWithQb: number;
  gameWeights: Record<string, number>;
  onGameWeights: (v: Record<string, number>) => void;
  excluded: string[];
  onExcluded: (v: string[]) => void;
}) {
  const stats = useQuery({
    queryKey: ["skeleton-stats", slateId, contestId],
    queryFn: () => api.get<SkeletonStatsResponse>(
      `/api/slates/${slateId}/skeleton-stats${contestId ? `?contest_id=${contestId}` : ""}`),
    enabled,
    staleTime: 60_000,
  });

  const [neff, setNeff] = useState<SkeletonNeff | null>(null);
  const [pending, setPending] = useState(false);
  const timer = useRef<number | undefined>(undefined);
  const depKey = JSON.stringify([nLineups, contestId, shapes, dstWithQb, gameWeights, excluded]);
  useEffect(() => {
    if (!enabled || !stats.data) return;
    setPending(true);
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(async () => {
      try {
        const r = await api.post<SkeletonNeff>(`/api/slates/${slateId}/skeleton-neff`, {
          n_lineups: nLineups,
          contest_id: contestId,
          shape_allocation: shapes,
          game_weights: gameWeights,
          skeleton_exclude: excluded,
          dst_with_qb_weight: dstWithQb,
        });
        setNeff(r);
      } catch { /* stale slate / sims evicted -- table still renders */ }
      setPending(false);
    }, 400);
    return () => window.clearTimeout(timer.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [depKey, enabled, stats.data]);

  const [open, setOpen] = useState<string | null>(null);
  if (!enabled) return null;
  if (stats.isError) {
    return <div className="text-[11px] text-[var(--amber)]">
      Skeleton browser needs the sims matrix — run Simulate first.
    </div>;
  }
  if (!stats.data) return <div className="text-[11px] text-[var(--dim)]">Enumerating skeletons…</div>;

  const byGame = new Map<string, SkeletonStat[]>();
  for (const sk of stats.data.skeletons) {
    if (!byGame.has(sk.game_id)) byGame.set(sk.game_id, []);
    byGame.get(sk.game_id)!.push(sk);
  }
  const games = [...stats.data.games].sort((a, b) => (b.total ?? 0) - (a.total ?? 0));
  const exSet = new Set(excluded);
  const setGw = (gid: string, v: number) =>
    onGameWeights({ ...gameWeights, [gid]: Math.max(0, v) });
  const toggle = (key: string) =>
    onExcluded(exSet.has(key) ? excluded.filter((k) => k !== key) : [...excluded, key]);

  return (
    <div className="space-y-2">
      <div className="flex items-baseline gap-3 flex-wrap">
        <span className="eyebrow">Skeleton allocation</span>
        <span className="text-[10px] text-[var(--dim)]">
          game emphasis multiplies the shape mix · 0 silences a game · basis:{" "}
          {stats.data.basis === "payout" ? "expected payout vs sampled field" : "tail mass (no field/contest)"}
        </span>
        <span className="ml-auto num text-sm">
          structural N<sub>eff</sub>{" "}
          <span className={pending ? "opacity-40" : ""}>{neff ? num(neff.n_eff) : "—"}</span>
          {neff && <span className="text-[10px] text-[var(--dim)]"> · {neff.n_active} skeletons live</span>}
        </span>
      </div>

      <div className="panel divide-y divide-[var(--line)]">
        {games.map((g) => {
          const sks = (byGame.get(g.game_id) ?? []).filter((s) => s.feasible);
          const n = neff?.by_game[g.game_id] ?? 0;
          const w = gameWeights[g.game_id] ?? 1;
          const nExcluded = sks.filter((s) => exSet.has(s.key)).length;
          return (
            <div key={g.game_id}>
              <div className="flex items-center gap-3 px-3 py-1.5">
                <button type="button" className="text-left flex items-center gap-3 flex-1 hover:text-[var(--ink)]"
                  onClick={() => setOpen(open === g.game_id ? null : g.game_id)}>
                  <span className="num text-[var(--dim)] w-3">{open === g.game_id ? "−" : "+"}</span>
                  <span className="font-medium w-24">{g.away} @ {g.home}</span>
                  <span className="num text-[11px] text-[var(--dim)] w-28">
                    {num(g.away_implied)} / {num(g.home_implied)} · {num(g.total)}
                  </span>
                  {n > 0 && <span className="num text-[11px]">{n} lineups</span>}
                  {nExcluded > 0 && <Badge tone="amber">{nExcluded} excluded</Badge>}
                  {w === 0 && <Badge tone="down">silenced</Badge>}
                </button>
                <label className="flex items-center gap-1 text-[10px] text-[var(--dim)]">
                  ×
                  <input type="number" min={0} step={0.5} value={w}
                    onChange={(e) => setGw(g.game_id, Number(e.target.value))}
                    className="w-14 px-1 py-0.5 text-right" />
                </label>
              </div>
              {open === g.game_id && (
                <table className="w-full text-[11px]">
                  <thead>
                    <tr className="text-[9px] uppercase tracking-wider text-[var(--dim)]">
                      <th className="text-left px-3 py-1">skeleton</th>
                      <th className="text-right px-2">mean</th>
                      <th className="text-right px-2">ceil</th>
                      <th className="text-right px-2">own%</th>
                      <th className="text-right px-2">lineups</th>
                      <th className="text-right px-2" title="leave-one-out N_eff delta">ΔN eff</th>
                      <th className="text-right px-3">off</th>
                    </tr>
                  </thead>
                  <tbody>
                    {sks
                      .sort((a, b) => (neff?.counts[b.key] ?? 0) - (neff?.counts[a.key] ?? 0)
                        || b.default_weight - a.default_weight)
                      .map((sk) => {
                        const c = neff?.counts[sk.key] ?? 0;
                        const ex = exSet.has(sk.key);
                        return (
                          <tr key={sk.key}
                            className={`border-t hairline ${ex ? "opacity-40" : c === 0 ? "opacity-60" : ""}`}>
                            <td className="px-3 py-0.5 whitespace-nowrap">
                              {sk.qb_team} {sk.n_teammates > 0 && `+${sk.n_teammates}`}
                              {sk.n_bringback > 0 && ` vs ${sk.n_bringback}`}
                              {sk.dst_with_qb && <span className="text-[var(--dim)]"> +DST</span>}
                              <span className="text-[9px] text-[var(--dim)] ml-1.5">
                                {sk.display.split(" ").slice(1).join(" ")}
                              </span>
                            </td>
                            <td className="num text-right px-2">{num(sk.mean)}</td>
                            <td className="num text-right px-2">{num(sk.ceiling)}</td>
                            <td className="num text-right px-2">{num(sk.ownership, 0)}</td>
                            <td className="num text-right px-2">{c || "—"}</td>
                            <td className="num text-right px-2">
                              {neff?.contributions[sk.key] != null ? num(neff.contributions[sk.key], 2) : "—"}
                            </td>
                            <td className="text-right px-3">
                              <input type="checkbox" checked={ex} onChange={() => toggle(sk.key)} />
                            </td>
                          </tr>
                        );
                      })}
                  </tbody>
                </table>
              )}
            </div>
          );
        })}
      </div>
      {neff && Object.keys(neff.by_shape).length > 0 && (
        <div className="text-[10px] text-[var(--dim)]">
          expected mix:{" "}
          {Object.entries(neff.by_shape).sort((a, b) => b[1] - a[1])
            .map(([k, v]) => `${k} ${v}`).join(" · ")}
        </div>
      )}
    </div>
  );
}
