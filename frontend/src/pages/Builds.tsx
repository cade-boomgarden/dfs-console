import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { api, Job, watchJob } from "../api";
import { MODEL_DEFAULT, ShapeAllocation } from "../components/ShapeAllocation";
import { Badge, Btn, Field, num, Progress } from "../ui";

interface SetRow {
  id: number; kind: string; label: string; status: string; n_lineups: number;
  n_eff: number | null; n_eff_flag: boolean; created_at: string; stale_pool: boolean;
}

export default function Builds() {
  const { slateId } = useParams();
  const qc = useQueryClient();
  const slate = useQuery({ queryKey: ["slate", slateId], queryFn: () => api.get<any>(`/api/slates/${slateId}`) });
  const sets = useQuery({ queryKey: ["sets", slateId], queryFn: () => api.get<SetRow[]>(`/api/slates/${slateId}/sets`) });
  const [cfg, setCfg] = useState({
    n_lineups: 150, n_candidates: 900, sim_block: 30, max_overlap: 6,
    global_max_exposure: 0.5, max_repeat_qb: 25, min_projection: 4, label: "",
  });
  const [job, setJob] = useState<Job | null>(null);
  const [shapes, setShapes] = useState<Record<string, number>>({ ...MODEL_DEFAULT });
  const [dstWithQb, setDstWithQb] = useState(0.25);

  const pv = slate.data?.pool_versions?.find((v: any) => v.is_current);

  const run = async () => {
    const { job_id } = await api.post<{ job_id: number }>(`/api/slates/${slateId}/build`, {
      pool_version_id: pv.id,
      config: {
        ...cfg,
        label: cfg.label || `Build x${cfg.n_lineups}`,
        shape_allocation: shapes,
        dst_with_qb_weight: dstWithQb,
      },
    });
    watchJob(job_id, (j) => {
      setJob(j);
      if (j.status === "done") qc.invalidateQueries({ queryKey: ["sets", slateId] });
    });
  };

  const set = (k: string) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setCfg({ ...cfg, [k]: e.target.type === "number" ? Number(e.target.value) : e.target.value });

  return (
    <div className="space-y-6">
      <section className="panel p-4 space-y-3">
        <h2 className="eyebrow">New build — Stage A skeleton-seeded, Stage B top-N unique</h2>
        <div className="grid grid-cols-4 md:grid-cols-7 gap-3">
          <Field label="Lineups"><input type="number" value={cfg.n_lineups} onChange={set("n_lineups")} /></Field>
          <Field label="Candidates"><input type="number" value={cfg.n_candidates} onChange={set("n_candidates")} /></Field>
          <Field label="Sim block"><input type="number" value={cfg.sim_block} onChange={set("sim_block")} /></Field>
          <Field label="Max overlap"><input type="number" value={cfg.max_overlap} onChange={set("max_overlap")} /></Field>
          <Field label="Max exposure"><input type="number" step={0.05} value={cfg.global_max_exposure} onChange={set("global_max_exposure")} /></Field>
          <Field label="Max repeat QB"><input type="number" value={cfg.max_repeat_qb} onChange={set("max_repeat_qb")} /></Field>
          <Field label="Min proj"><input type="number" value={cfg.min_projection} onChange={set("min_projection")} /></Field>
        </div>
        <div className="border-t hairline pt-3">
          <ShapeAllocation value={shapes} onChange={setShapes} />
          <div className="mt-2 flex items-center gap-2">
            <span className="eyebrow">DST with QB</span>
            <input type="number" step={0.05} min={0} className="w-20"
              value={dstWithQb} onChange={(e) => setDstWithQb(Number(e.target.value))} />
            <span className="text-[10px] text-[var(--dim)]">
              multiplier applied when the DST is the QB's own team — 0 excludes it
            </span>
          </div>
        </div>

        <div className="flex items-end gap-3">
          <Field label="Label"><input value={cfg.label} onChange={set("label")} placeholder="Sunday main 150-max" className="w-64" /></Field>
          <Btn kind="primary" onClick={run} disabled={!pv?.has_sims || (job !== null && job.status === "running")}>
            Run build
          </Btn>
          {!pv?.has_sims && <span className="text-[11px] text-[var(--amber)]">Sims matrix required — see Overview.</span>}
        </div>
        {job && job.status !== "done" && <Progress value={job.progress} message={`${job.status} — ${job.message}`} />}
        {job?.status === "done" && (
          <div className="space-y-1">
            <div className="text-xs">
              Built {String(job.result.n_lineups)} lineups from {String(job.result.n_candidates)} candidates ·
              N<sub>eff</sub> {String(job.result.n_eff)}
              <span className="text-[var(--dim)]"> / {String(job.result.n_eff_random)} random baseline</span>
            </div>
            {job.result.shape_mix != null && (
              <div className="text-[11px] text-[var(--dim)]">
                delivered:{" "}
                {Object.entries(job.result.shape_mix as Record<string, number>)
                  .sort((a, b) => b[1] - a[1])
                  .map(([k, v]) => `${k} ${v}`).join(" · ")}
              </div>
            )}
          </div>
        )}
      </section>

      <section>
        <h2 className="eyebrow mb-2">Lineup sets</h2>
        <div className="panel divide-y divide-[var(--line)]">
          {sets.data?.map((s) => (
            <Link key={s.id} to={`/slate/${slateId}/sets/${s.id}`}
              className="flex items-center gap-3 px-4 py-2.5 hover:bg-[var(--raised)]">
              <span className="num text-[var(--dim)]">#{s.id}</span>
              <span className="font-medium">{s.label || s.kind}</span>
              <Badge>{s.kind}</Badge>
              <span className="num text-[var(--dim)]">{s.n_lineups} lineups</span>
              {s.n_eff !== null && (
                <span className="num">
                  N<sub>eff</sub> <span className={s.n_eff_flag ? "text-[var(--down)]" : "text-[var(--up)]"}>{num(s.n_eff)}</span>
                </span>
              )}
              {s.n_eff_flag && <Badge tone="down">lookalikes</Badge>}
              {s.stale_pool && <Badge tone="amber">built on data that has since changed</Badge>}
              <span className="ml-auto text-[11px] text-[var(--dim)]">{s.created_at.slice(0, 19)}</span>
            </Link>
          ))}
          {sets.data?.length === 0 && <div className="px-4 py-6 text-[var(--dim)]">No lineup sets yet.</div>}
        </div>
      </section>
    </div>
  );
}
