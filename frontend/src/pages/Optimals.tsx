import { useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { api, Job, watchJob } from "../api";
import { Badge, Btn, Field, money, num, Progress } from "../ui";

/** Construction optimals: the top X lineups for each roster construction
 *  (QB x stack x bringbacks x DST x salary shape), under mean, median and
 *  ceiling. A read on what the field's optimizers surface, not an entry
 *  builder. */

interface QB { id: number; name: string; team: string; opp: string; game: string;
  salary: number; projection: number; starter: boolean }
interface Bands { lo: number; hi: number; quantiles: number[]; n_reference: number }
interface RunResult {
  pool_version_id: number; n_constructions: number; n_infeasible: number;
  n_lineups: number; n_players_used: number; seconds: number; bands: Bands;
  has_ownership: boolean;
  config: RunConfig;
  stats: { solves: number; non_optimal: number;
           ms_per_solve_any: number | null; ms_per_solve_band: number | null };
}
interface Options {
  pool_version_id: number; has_sims: boolean; qbs: QB[];
  ms_per_solve: { any: number; band: number }; max_seconds: number;
  latest: { job_id: number; finished_at: string; result: RunResult;
            stale: string | null; available: boolean } | null;
  running_job_id: number | null;
}
interface RunConfig {
  qb_ids: number[]; teammates: number[]; bringbacks: number[]; dst: boolean[];
  shapes: string[]; top_x: number; min_diff: number; use_adjustments: boolean;
}
interface PlayerInfo { name: string; pos: string; team: string; opp: string;
  salary: number; own: number; mean: number; median: number; ceiling: number }
interface Row {
  ids: string[]; slots: string[]; qb_id: string; qb_team: string;
  n_teammates: number; n_bringback: number; stack: string; dst_with_qb: boolean;
  shape: "balanced" | "middle" | "studs_duds"; salary_sd: number; salary: number;
  projection: number; median: number | null; ceiling: number | null; floor: number | null;
  own_sum: number; own_log_prod: number; objectives: string[];
  hits: { c: string; o: string; r: number }[]; best_rank: number; description: string;
}
interface Doc {
  bands: Bands; players: Record<string, PlayerInfo>; lineups: Row[];
  infeasible: { construction: string; reason: string }[];
}

const STACKS = [[0, "Naked"], [1, "Single"], [2, "Double"], [3, "Onslaught"]] as const;
const SHAPES = [["any", "Any"], ["balanced", "Balanced"], ["middle", "Middle"],
  ["studs_duds", "Studs/duds"]] as const;
const SHAPE_LABEL: Record<string, string> = { balanced: "Balanced", middle: "Middle", studs_duds: "Studs/duds" };
const OBJ_SHORT: Record<string, string> = { mean: "Mean", median: "Med", ceiling: "Ceil" };
const PAGE = 200;

type SortKey = "n_bringback" | "projection" | "ceiling" | "floor" | "salary"
  | "own_sum" | "own_log_prod";

function Chip({ on, onClick, children, title }: {
  on: boolean; onClick: () => void; children: React.ReactNode; title?: string;
}) {
  return (
    <button type="button" onClick={onClick} title={title} aria-pressed={on}
      className={`px-2 py-0.5 rounded text-xs border ${on
        ? "border-[var(--ink)] text-[var(--ink)] bg-[var(--raised)]"
        : "border-[var(--line)] text-[var(--dim)] hover:text-[var(--ink)]"}`}>
      {children}
    </button>
  );
}

function toggle<T>(list: T[], v: T): T[] {
  return list.includes(v) ? list.filter((x) => x !== v) : [...list, v];
}

export default function Optimals() {
  const { slateId } = useParams();
  const qc = useQueryClient();
  const opts = useQuery({ queryKey: ["optimals", slateId],
    queryFn: () => api.get<Options>(`/api/slates/${slateId}/optimals`) });
  const latestId = opts.data?.latest?.available ? opts.data.latest.job_id : null;
  const data = useQuery({ queryKey: ["optimals-data", slateId, latestId],
    enabled: latestId !== null,
    queryFn: () => api.get<Doc>(`/api/slates/${slateId}/optimals/data`) });

  // ---------------- run form ----------------
  const [qbIds, setQbIds] = useState<number[] | null>(null);
  const [teammates, setTeammates] = useState<number[]>([0, 1, 2]);
  const [bringbacks, setBringbacks] = useState<number[]>([0, 1, 2]);
  const [dst, setDst] = useState<boolean[]>([false]);
  const [shapes, setShapes] = useState<string[]>(["any"]);
  const [topX, setTopX] = useState(10);
  const [minDiff, setMinDiff] = useState(1);
  const [useAdj, setUseAdj] = useState(false);
  const [job, setJob] = useState<Job | null>(null);
  const [runError, setRunError] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);

  const starters = useMemo(() => (opts.data?.qbs ?? []).filter((q) => q.starter).map((q) => q.id),
    [opts.data]);
  const selectedQbs = qbIds ?? starters;

  const follow = (id: number) => watchJob(id, (j) => {
    setJob(j);
    if (j.status === "done") {
      qc.invalidateQueries({ queryKey: ["optimals", slateId] });
      qc.invalidateQueries({ queryKey: ["optimals-data", slateId] });
    }
  });
  useEffect(() => {
    const id = opts.data?.running_job_id;
    if (id && !job) return follow(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [opts.data?.running_job_id]);

  const cells = selectedQbs.length * teammates.length * bringbacks.length * dst.length;
  const anyCells = shapes.includes("any") ? cells : 0;
  const bandCells = cells * shapes.filter((s) => s !== "any").length;
  const solves = (anyCells + bandCells) * 3 * topX;
  const rate = opts.data?.ms_per_solve ?? { any: 70, band: 220 };
  const estSec = 15 + (anyCells * 3 * topX * rate.any + bandCells * 3 * topX * rate.band) / 1000;
  const overCap = estSec > (opts.data?.max_seconds ?? Infinity);
  const running = job !== null && !["done", "failed", "cancelled"].includes(job.status);

  const run = async () => {
    setRunError(null);
    try {
      const { job_id } = await api.post<{ job_id: number }>(`/api/slates/${slateId}/optimals/run`, {
        qb_ids: selectedQbs, teammates, bringbacks, dst, shapes,
        top_x: topX, min_diff: minDiff, use_adjustments: useAdj,
      });
      setShowForm(false);
      follow(job_id);
    } catch (e) {
      setRunError((e as Error).message);
    }
  };

  const games = useMemo(() => {
    const m = new Map<string, QB[]>();
    for (const q of opts.data?.qbs ?? []) m.set(q.game, [...(m.get(q.game) ?? []), q]);
    return [...m.values()].map((qs) => [...qs].sort((a, b) => a.team.localeCompare(b.team)
      || b.projection - a.projection));
  }, [opts.data]);

  // ---------------- results filters ----------------
  const [playerQ, setPlayerQ] = useState("");
  const [objF, setObjF] = useState("ALL");
  const [qbF, setQbF] = useState("ALL");
  const [stackF, setStackF] = useState("ALL");
  const [bbF, setBbF] = useState("ALL");
  const [dstF, setDstF] = useState("ALL");
  const [shapeF, setShapeF] = useState("ALL");
  const [sort, setSort] = useState<SortKey>("projection");
  const [desc, setDesc] = useState(true);
  const [shown, setShown] = useState(PAGE);

  const doc = data.data;
  const rows = useMemo(() => {
    if (!doc) return [];
    const terms = playerQ.split(",").map((t) => t.trim().toLowerCase()).filter(Boolean);
    const list = doc.lineups.filter((lu) => {
      if (objF !== "ALL" && !lu.objectives.includes(objF)) return false;
      if (qbF !== "ALL" && lu.qb_id !== qbF) return false;
      if (stackF !== "ALL" && lu.stack !== stackF) return false;
      if (bbF !== "ALL" && lu.n_bringback !== Number(bbF)) return false;
      if (dstF !== "ALL" && lu.dst_with_qb !== (dstF === "yes")) return false;
      if (shapeF !== "ALL" && lu.shape !== shapeF) return false;
      return terms.every((t) => lu.ids.some((i) => doc.players[i]?.name.toLowerCase().includes(t)));
    });
    const v = (lu: Row) => (lu[sort] ?? -Infinity) as number;
    return list.sort((a, b) => (desc ? v(b) - v(a) : v(a) - v(b)));
  }, [doc, playerQ, objF, qbF, stackF, bbF, dstF, shapeF, sort, desc]);
  useEffect(() => setShown(PAGE), [rows]);

  const usedInView = useMemo(() => new Set(rows.flatMap((r) => r.ids)).size, [rows]);
  const qbOptions = useMemo(() => {
    if (!doc) return [];
    const ids = [...new Set(doc.lineups.map((l) => l.qb_id))];
    return ids.map((i) => ({ id: i, name: doc.players[i]?.name ?? i, team: doc.players[i]?.team ?? "" }))
      .sort((a, b) => a.team.localeCompare(b.team));
  }, [doc]);

  const Th = ({ k, children, title }: { k?: SortKey; children: React.ReactNode; title?: string }) => (
    <th title={title}
      onClick={k ? () => { sort === k ? setDesc(!desc) : (setSort(k), setDesc(true)); } : undefined}
      className={`px-2 py-1.5 text-left text-[10px] uppercase tracking-wider text-[var(--dim)] whitespace-nowrap
        ${k ? "cursor-pointer select-none hover:text-[var(--ink)]" : ""}`}>
      {children}{k && sort === k ? (desc ? " ↓" : " ↑") : ""}
    </th>
  );

  if (!opts.data) return opts.isError
    ? <div className="text-[var(--dim)]">{(opts.error as Error).message}</div> : null;
  const o = opts.data;
  const latest = o.latest;
  const res = latest?.result;

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3 flex-wrap">
        <h1 className="text-lg font-semibold">Construction optimals</h1>
        <span className="text-[11px] text-[var(--dim)]">
          Top lineups per roster construction, under mean, median and ceiling. A read on what
          optimizers surface, not entries.
        </span>
        <span className="ml-auto">
          <Btn kind={latest ? "default" : "primary"} disabled={running}
            onClick={() => setShowForm(!showForm)}>
            {showForm ? "Hide run settings" : latest ? "New run…" : "Set up a run…"}
          </Btn>
        </span>
      </div>

      {!o.has_sims && (
        <div className="panel px-3 py-2 text-[var(--dim)]">
          Run Simulate on the Overview tab first. Median, ceiling and the lineup stats come from the sims.
        </div>
      )}

      {running && job && (
        <div className="flex items-center gap-3">
          <div className="flex-1"><Progress value={job.progress} message={job.message} /></div>
          <Btn kind="ghost" onClick={() => api.post(`/api/jobs/${job.id}/cancel`)}>Cancel</Btn>
        </div>
      )}
      {job?.status === "cancelled" && (
        <div className="text-[11px] text-[var(--dim)]">Run cancelled. The previous results are unchanged.</div>
      )}
      {job?.status === "failed" && (
        <div className="panel px-3 py-2 text-[11px] whitespace-pre-wrap">
          Run failed: {job.message.trim().split("\n").slice(-1)[0]}
        </div>
      )}

      {/* ---------------- run form ---------------- */}
      {(showForm || (!latest && !running)) && o.has_sims && (
        <section className="panel p-3 space-y-3">
          <div>
            <div className="flex items-center gap-3 mb-1.5">
              <span className="eyebrow">Quarterbacks ({selectedQbs.length})</span>
              <button type="button" className="text-[10px] text-[var(--dim)] hover:text-[var(--ink)]"
                onClick={() => setQbIds(starters)}>starters</button>
              <button type="button" className="text-[10px] text-[var(--dim)] hover:text-[var(--ink)]"
                onClick={() => setQbIds(o.qbs.map((q) => q.id))}>all</button>
              <button type="button" className="text-[10px] text-[var(--dim)] hover:text-[var(--ink)]"
                onClick={() => setQbIds([])}>none</button>
            </div>
            <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-1.5">
              {games.map((qs) => (
                <div key={qs[0].game} className="flex flex-wrap gap-1 items-center">
                  {qs.map((q) => (
                    <Chip key={q.id} on={selectedQbs.includes(q.id)}
                      onClick={() => setQbIds(toggle(selectedQbs, q.id))}
                      title={`${q.team} vs ${q.opp} · ${money(q.salary)}`}>
                      {q.name} <span className="text-[var(--mute)]">{q.team} {num(q.projection)}</span>
                    </Chip>
                  ))}
                </div>
              ))}
            </div>
          </div>

          <div className="flex flex-wrap gap-6">
            <div>
              <div className="eyebrow mb-1.5" title="QB-team RB/WR/TE rostered with the QB, exact count">Stack</div>
              <div className="flex gap-1">
                {STACKS.map(([v, l]) => (
                  <Chip key={v} on={teammates.includes(v)} onClick={() => setTeammates(toggle(teammates, v))}>
                    {l}
                  </Chip>
                ))}
              </div>
            </div>
            <div>
              <div className="eyebrow mb-1.5" title="Opposing RB/WR/TE from the QB's game, exact count">Bringbacks</div>
              <div className="flex gap-1">
                {[0, 1, 2].map((v) => (
                  <Chip key={v} on={bringbacks.includes(v)} onClick={() => setBringbacks(toggle(bringbacks, v))}>
                    {v}
                  </Chip>
                ))}
              </div>
            </div>
            <div>
              <div className="eyebrow mb-1.5">DST with QB</div>
              <div className="flex gap-1">
                {[false, true].map((v) => (
                  <Chip key={String(v)} on={dst.includes(v)} onClick={() => setDst(toggle(dst, v))}>
                    {v ? "Yes" : "No"}
                  </Chip>
                ))}
              </div>
            </div>
            <div>
              <div className="eyebrow mb-1.5"
                title="Bands on within-lineup salary SD, set per slate from 200 near-optimal reference lineups (20th and 80th percentiles)">
                Salary shape
              </div>
              <div className="flex gap-1">
                {SHAPES.map(([v, l]) => (
                  <Chip key={v} on={shapes.includes(v)} onClick={() => setShapes(toggle(shapes, v))}>
                    {l}
                  </Chip>
                ))}
              </div>
            </div>
          </div>

          <div className="flex flex-wrap items-end gap-4">
            <Field label="Top X per construction">
              <input type="number" min={1} max={50} value={topX} className="w-20"
                onChange={(e) => setTopX(Math.min(50, Math.max(1, Number(e.target.value) || 1)))} />
            </Field>
            <Field label="Min players different">
              <select value={minDiff} onChange={(e) => setMinDiff(Number(e.target.value))}>
                {[1, 2, 3, 4].map((v) => <option key={v} value={v}>{v}</option>)}
              </select>
            </Field>
            <label className="flex items-center gap-2 text-xs pb-1">
              <input type="checkbox" checked={useAdj} onChange={(e) => setUseAdj(e.target.checked)} />
              Use my Pool page adjustments and excludes
            </label>
            <div className="ml-auto text-right">
              <div className="num text-xs">
                {(cells * shapes.length).toLocaleString()} constructions × 3 objectives × {topX} ={" "}
                <span className="text-[var(--ink)]">{solves.toLocaleString()}</span> solves
              </div>
              <div className="text-[10px] text-[var(--dim)]">
                {overCap ? `About ${Math.round(estSec / 60)} min, over the ${Math.round(o.max_seconds / 60)}-min cap. Narrow the selection or lower top X.`
                  : `About ${estSec < 90 ? `${Math.round(estSec)} s` : `${Math.round(estSec / 60)} min`}`}
              </div>
            </div>
            <Btn kind="primary" disabled={running || overCap || solves === 0} onClick={run}>Run</Btn>
          </div>
          {runError && <div className="text-[11px]">{runError}</div>}
        </section>
      )}

      {/* ---------------- results ---------------- */}
      {latest && res && (
        <section className="panel">
          <div className="flex items-center gap-x-4 gap-y-1 px-3 py-2 border-b hairline flex-wrap text-[11px]">
            <span className="eyebrow">Last run</span>
            <span className="num text-[var(--dim)]">{latest.finished_at.slice(0, 16).replace("T", " ")}</span>
            {latest.stale && <Badge tone="amber">stale</Badge>}
            {latest.stale && <span className="text-[var(--dim)]">{latest.stale}</span>}
            <span className="num">{res.n_lineups.toLocaleString()} lineups</span>
            <span className="num">{res.n_players_used} players used</span>
            <span className="num text-[var(--dim)]">
              {res.n_constructions.toLocaleString()} constructions
              {res.n_infeasible ? `, ${res.n_infeasible} infeasible` : ""}
            </span>
            <span className="num text-[var(--dim)]">
              top {res.config.top_x} · min {res.config.min_diff} different
              {res.config.use_adjustments ? " · your adjustments" : " · raw projections"}
            </span>
            <span className="num text-[var(--dim)]" title={`Within-lineup salary SD; edges are the 20th/80th percentiles of ${res.bands.n_reference} reference lineups on this slate`}>
              Balanced ≤ {money(res.bands.lo)} &lt; Middle ≤ {money(res.bands.hi)} &lt; Studs/duds
            </span>
            <span className="num text-[var(--dim)]">{res.seconds.toFixed(0)} s</span>
            {res.stats.non_optimal > 0 && (
              <span title="Solves that hit the time limit: kept, but not proven best">
                {res.stats.non_optimal} unproven
              </span>
            )}
          </div>

          {res.has_ownership === false && (
            <div className="px-3 py-2 border-b hairline text-[11px] text-[var(--dim)]">
              The pool had no ownership when this ran, so Own Σ and Own ∏ are empty. Run the ownership job, then run this again.
            </div>
          )}
          {!latest.available && (
            <div className="px-3 py-2 text-[var(--dim)]">This run's results were pruned with its pool version. Run it again.</div>
          )}
          {data.isLoading && latest.available && <div className="px-3 py-2 text-[var(--dim)]">Loading lineups…</div>}

          {doc && (
            <>
              <div className="flex items-center gap-2 px-3 py-2 border-b hairline flex-wrap">
                <input placeholder="Players (comma = all of)…" value={playerQ}
                  onChange={(e) => setPlayerQ(e.target.value)} className="w-56" />
                <select value={objF} onChange={(e) => setObjF(e.target.value)} aria-label="Objective">
                  <option value="ALL">All objectives</option>
                  <option value="mean">Mean</option>
                  <option value="median">Median</option>
                  <option value="ceiling">Ceiling</option>
                </select>
                <select value={qbF} onChange={(e) => setQbF(e.target.value)} aria-label="QB">
                  <option value="ALL">All QBs</option>
                  {qbOptions.map((q) => <option key={q.id} value={q.id}>{q.team} {q.name}</option>)}
                </select>
                <select value={stackF} onChange={(e) => setStackF(e.target.value)} aria-label="Stack">
                  <option value="ALL">All stacks</option>
                  {STACKS.map(([, l]) => <option key={l} value={l}>{l}</option>)}
                </select>
                <select value={bbF} onChange={(e) => setBbF(e.target.value)} aria-label="Bringbacks">
                  <option value="ALL">Any bringbacks</option>
                  {[0, 1, 2].map((v) => <option key={v} value={v}>{v} bringback{v === 1 ? "" : "s"}</option>)}
                </select>
                <select value={dstF} onChange={(e) => setDstF(e.target.value)} aria-label="DST with QB">
                  <option value="ALL">Any DST</option>
                  <option value="no">DST elsewhere</option>
                  <option value="yes">DST with QB</option>
                </select>
                <select value={shapeF} onChange={(e) => setShapeF(e.target.value)} aria-label="Salary shape">
                  <option value="ALL">Any salary shape</option>
                  {SHAPES.filter(([v]) => v !== "any").map(([v, l]) => <option key={v} value={v}>{l}</option>)}
                </select>
                <span className="ml-auto num text-[11px] text-[var(--dim)]">
                  {rows.length.toLocaleString()} of {doc.lineups.length.toLocaleString()} · {usedInView} players
                </span>
              </div>

              <div className="overflow-auto max-h-[70vh]">
                <table className="w-full">
                  <thead className="sticky top-0 bg-[var(--panel)]">
                    <tr className="border-b hairline">
                      <Th>Build</Th>
                      <Th k="n_bringback">BB</Th>
                      <Th k="projection">Proj</Th>
                      <Th k="ceiling" title="p85 of the lineup's summed sims">Ceil</Th>
                      <Th k="floor" title="p20 of the lineup's summed sims">Floor</Th>
                      <Th k="salary">Salary</Th>
                      <Th k="own_sum">Own Σ</Th>
                      <Th k="own_log_prod"
                        title="log10 of the product of ownership fractions. Closer to 0 = more likely the field duplicates it. Assumes players are drafted independently, so it understates duplication of chalk stacks.">
                        Own ∏ log
                      </Th>
                      <Th title="Objectives this lineup ranked under, with its best rank">Found by</Th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.slice(0, shown).map((lu) => (
                      <tr key={lu.ids.join("-")} className="border-b hairline hover:bg-[var(--raised)] align-top">
                        <td className="px-2 py-1.5 max-w-[560px]">
                          <div className="text-[12px]">{lu.description}</div>
                          <div className="text-[11px] text-[var(--dim)]">
                            {lu.ids.map((i, k) => (
                              <span key={i}>
                                {k > 0 && " · "}
                                <span className="text-[var(--mute)]">{lu.slots[k]}</span> {doc.players[i]?.name}
                              </span>
                            ))}
                          </div>
                        </td>
                        <td className="px-2 py-1.5 num">{lu.n_bringback}</td>
                        <td className="px-2 py-1.5 num">{num(lu.projection)}</td>
                        <td className="px-2 py-1.5 num">{num(lu.ceiling)}</td>
                        <td className="px-2 py-1.5 num text-[var(--dim)]">{num(lu.floor)}</td>
                        <td className="px-2 py-1.5 num">
                          {money(lu.salary)}
                          <div className="text-[10px] text-[var(--dim)]">
                            {SHAPE_LABEL[lu.shape]} · SD {money(lu.salary_sd)}
                          </div>
                        </td>
                        <td className="px-2 py-1.5 num">{num(lu.own_sum, 0)}%</td>
                        <td className="px-2 py-1.5 num">{num(lu.own_log_prod, 1)}</td>
                        <td className="px-2 py-1.5 num text-[11px] whitespace-nowrap">
                          {lu.objectives.map((ob) => {
                            const best = Math.min(...lu.hits.filter((h) => h.o === ob).map((h) => h.r));
                            return <div key={ob}>{OBJ_SHORT[ob]} <span className="text-[var(--dim)]">#{best}</span></div>;
                          })}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {rows.length > shown && (
                  <div className="p-2 text-center">
                    <Btn kind="ghost" onClick={() => setShown(shown + PAGE)}>
                      Show {Math.min(PAGE, rows.length - shown)} more
                    </Btn>
                  </div>
                )}
              </div>
              {doc.infeasible.length > 0 && (
                <details className="px-3 py-2 border-t hairline text-[11px] text-[var(--dim)]">
                  <summary className="cursor-pointer">{doc.infeasible.length} constructions had no legal lineup</summary>
                  <ul className="mt-1 num">
                    {doc.infeasible.map((f) => {
                      const [qb, t, b, d, s] = f.construction.split("|");
                      const p = doc.players[qb];
                      return (
                        <li key={f.construction}>
                          {p ? `${p.team} ${p.name}` : qb} · {STACKS[Number(t)]?.[1] ?? t} · {b} BB
                          {d === "1" ? " · DST w/ QB" : ""}{s && s !== "any" ? ` · ${SHAPE_LABEL[s]}` : ""}: {f.reason}
                        </li>
                      );
                    })}
                  </ul>
                </details>
              )}
            </>
          )}
        </section>
      )}
    </div>
  );
}
