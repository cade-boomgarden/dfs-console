import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, Job, watchJob } from "../api";
import { Btn, Field, Progress } from "../ui";

interface Coverage {
  season: number | null; week: number | null; draft_group_id: number;
  slate_date: string | null; source: string; players: number; projected: number;
}
interface WeekResult {
  season: number; week: number; sunday: string; draft_group_id?: number;
  n_games?: number; shape?: string; salaries?: number; probes?: number;
  estimated_id?: number; error?: string;
  projected?: number; no_projection_count?: number; no_projection?: string[];
  projection_error?: string;
  candidates?: Record<string, { n_games: number; shape: string; first_kick: string; last_kick: string }>;
}

/** Salary coverage by week, and a backfill for weeks that were never
 *  ingested. The backfill finds each week's main slate by probing DraftKings
 *  draft group ids, then stores salaries only (no slate, no projections). */
export default function SalaryBackfill({ season, currentWeek }: { season: number; currentWeek: number }) {
  const qc = useQueryClient();
  const cov = useQuery({ queryKey: ["salary-coverage"],
    queryFn: () => api.get<Coverage[]>("/api/salaries/coverage") });
  const covered = useMemo(() => {
    const m = new Map<number, Coverage>();
    (cov.data ?? []).filter((c) => c.season === season && c.week)
      .forEach((c) => { if (!m.has(c.week!) || c.source === "ingest") m.set(c.week!, c); });
    return m;
  }, [cov.data, season]);
  const unprojected = useMemo(() => [...covered.values()]
    .filter((c) => c.source !== "ingest" && c.projected === 0).map((c) => c.week!).sort((a, b) => a - b),
  [covered]);
  const missing = useMemo(() => {
    const out: number[] = [];
    for (let w = 1; w <= Math.min(currentWeek, 18); w++) if (!covered.has(w)) out.push(w);
    return out;
  }, [covered, currentWeek]);

  const [weeks, setWeeks] = useState("");
  const [dg, setDg] = useState("");
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState("");
  const weekList = (weeks.trim() ? weeks : missing.join(","))
    .split(/[\s,]+/).filter(Boolean).map(Number).filter((w) => w >= 1 && w <= 18);

  const run = async () => {
    setError("");
    try {
      const { job_id } = await api.post<{ job_id: number }>("/api/salaries/backfill", {
        season, weeks: weekList, ...(dg ? { draft_group_id: Number(dg) } : {}),
      });
      watchJob(job_id, (j) => {
        setJob(j);
        if (j.status === "failed") setError(j.message);
        if (j.status === "done") qc.invalidateQueries({ queryKey: ["salary-coverage"] });
      });
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const fillProjections = async () => {
    setError("");
    try {
      const wl = weeks.trim() ? weekList : unprojected;
      const { job_id } = await api.post<{ job_id: number }>("/api/salaries/projections", { season, weeks: wl });
      watchJob(job_id, (j) => {
        setJob(j);
        if (j.status === "failed") setError(j.message);
        if (j.status === "done") qc.invalidateQueries({ queryKey: ["salary-coverage"] });
      });
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const results = (job?.status === "done" ? (job.result.weeks as WeekResult[]) : null) ?? [];
  const running = job?.status === "running" || job?.status === "queued";

  return (
    <section className="panel p-4 space-y-3">
      <div className="flex items-center gap-3">
        <h2 className="eyebrow">Salary history · {season}</h2>
        <div className="flex gap-1 ml-2">
          {Array.from({ length: 18 }, (_, i) => i + 1).map((w) => {
            const c = covered.get(w);
            const proj = c ? c.projected > 0 : false;
            return (
              <span key={w}
                title={c ? `wk${w}: DG ${c.draft_group_id}, ${c.players} salaries, ${c.projected} projections (${c.source})` : `wk${w}: no salaries`}
                className={`num text-[10px] w-6 text-center rounded py-0.5 border
                  ${c && proj ? "border-[var(--dim)] text-[var(--ink)] bg-[var(--raised)]"
                    : c ? "border-[var(--dim)] text-[var(--dim)]"
                    : w <= currentWeek ? "border-dashed border-[var(--mute)] text-[var(--mute)]" : "border-transparent text-[var(--mute)]"}`}>
                {w}
              </span>
            );
          })}
        </div>
      </div>
      <div className="flex items-end gap-3 flex-wrap">
        <Field label="Weeks">
          <input className="w-40" value={weeks} onChange={(e) => setWeeks(e.target.value)}
            placeholder={missing.length ? missing.join(",") : "e.g. 9,10"} />
        </Field>
        <Field label="Draft group (optional)">
          <input type="number" className="w-28" value={dg} placeholder="search"
            onChange={(e) => setDg(e.target.value)} />
        </Field>
        <Btn kind="primary" disabled={!weekList.length || running || (!!dg && weekList.length !== 1)}
          onClick={run}>
          Find and import salaries
        </Btn>
        <Btn disabled={running || (weeks.trim() ? !weekList.length : !unprojected.length)} onClick={fillProjections}>
          Fill projections{!weeks.trim() && unprojected.length ? ` (wk ${unprojected.join(",")})` : ""}
        </Btn>
      </div>
      <div className="text-[11px] text-[var(--dim)] max-w-2xl">
        Filled weeks have salaries and projections, outlined weeks salaries only, dashed weeks neither.
        Leave Weeks blank to fill all the dashed weeks for {season}; projections come along. Fill
        projections pulls FantasyPros for outlined weeks. The search
        probes DraftKings draft group ids outward from an estimate between your known slates and takes
        the Sunday 1pm group with the most games. It stores salaries only and adds nothing to the slate
        list. A draft group ID skips the search and needs exactly one week.
      </div>
      {job && running && <Progress value={job.progress} message={job.message} />}
      {error && <pre className="text-[11px] whitespace-pre-wrap border-l-2 border-[var(--line)] pl-2">{error}</pre>}
      {results.length > 0 && (
        <table className="text-[11px] num">
          <thead>
            <tr className="text-[9px] uppercase tracking-wider text-[var(--dim)]">
              {["Week", "Sunday", "Draft group", "Games", "Shape", "Salaries", "Projected", "Probes", "Other candidates"].map((h) =>
                <th key={h} className="text-left font-normal pr-4 pb-1">{h}</th>)}
            </tr>
          </thead>
          <tbody>
            {results.map((r) => (
              <tr key={`${r.week}-${r.draft_group_id ?? ""}`}>
                <td className="pr-4">wk{r.week}</td>
                <td className="pr-4 text-[var(--dim)]">{r.sunday ?? "—"}</td>
                {r.error
                  ? <td colSpan={7} className="pr-4">{r.error}</td>
                  : <>
                      <td className="pr-4">{r.draft_group_id}</td>
                      <td className="pr-4">{r.n_games}</td>
                      <td className="pr-4 text-[var(--dim)]">{r.shape}</td>
                      <td className="pr-4">{r.salaries ?? "—"}</td>
                      <td className="pr-4" title={r.no_projection?.length ? `No projection: ${r.no_projection.join(", ")}` : undefined}>
                        {r.projection_error ?? (r.projected !== undefined
                          ? `${r.projected}${r.no_projection_count ? ` · ${r.no_projection_count} active unmatched` : ""}` : "—")}
                      </td>
                      <td className="pr-4 text-[var(--dim)]">{r.probes ?? "—"}</td>
                      <td className="pr-4 text-[var(--dim)]">
                        {Object.entries(r.candidates ?? {})
                          .filter(([g]) => Number(g) !== r.draft_group_id)
                          .map(([g, c]) => `${g} (${c.n_games}g ${c.shape})`).join(", ") || "—"}
                      </td>
                    </>}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
