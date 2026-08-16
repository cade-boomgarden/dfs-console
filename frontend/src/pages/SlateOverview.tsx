import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { api, watchJob } from "../api";
import { Badge, Btn, num, Progress } from "../ui";

interface Detail {
  id: number; name: string; draft_group_id: number; start_time: string | null;
  games: { id: number; home: string; away: string; total: number | null;
    home_spread: number | null; home_implied: number | null; away_implied: number | null }[];
  pool_versions: { id: number; label: string; created_at: string; is_current: boolean;
    has_sims: boolean; n_sims: number | null }[];
}

export default function SlateOverview() {
  const { slateId } = useParams();
  const qc = useQueryClient();
  const d = useQuery({ queryKey: ["slate", slateId],
    queryFn: () => api.get<Detail>(`/api/slates/${slateId}`) });
  const [job, setJob] = useState<{ progress: number; message: string; status: string } | null>(null);

  const simulate = async (pvId: number) => {
    const { job_id } = await api.post<{ job_id: number }>("/api/slates/simulate", { pool_version_id: pvId });
    watchJob(job_id, (j) => {
      setJob(j);
      if (j.status === "done") qc.invalidateQueries();
    });
  };

  if (!d.data) return null;
  const games = [...d.data.games].sort((a, b) => (b.total ?? 0) - (a.total ?? 0));
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-lg font-semibold">{d.data.name}</h1>
        <div className="text-[var(--dim)] text-xs num">DG {d.data.draft_group_id} · {d.data.start_time ?? ""}</div>
      </div>
      {job && job.status !== "done" && <Progress value={job.progress} message={job.message} />}

      <section>
        <h2 className="eyebrow mb-2">Games — implied totals</h2>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
          {games.map((g) => (
            <div key={g.id} className="panel px-3 py-2">
              <div className="flex justify-between text-xs">
                <span>{g.away} @ {g.home}</span>
                <span className="num text-[var(--dim)]">O/U {num(g.total)}</span>
              </div>
              <div className="flex justify-between mt-1 num">
                <span>{g.away} <span className="text-[var(--up)]">{num(g.away_implied)}</span></span>
                <span>{g.home} <span className="text-[var(--up)]">{num(g.home_implied)}</span></span>
              </div>
            </div>
          ))}
          {games.length === 0 && <div className="text-[var(--dim)]">No games — ingest first.</div>}
        </div>
      </section>

      <section>
        <h2 className="eyebrow mb-2">Pool versions</h2>
        <div className="panel divide-y divide-[var(--line)]">
          {d.data.pool_versions.map((v) => (
            <div key={v.id} className="flex items-center gap-3 px-4 py-2">
              <span className="num">v{v.id}</span>
              <span className="text-[var(--dim)]">{v.label}</span>
              <span className="text-[var(--dim)] text-[11px]">{v.created_at.slice(0, 19)}</span>
              {v.is_current && <Badge tone="amber">current</Badge>}
              {v.has_sims
                ? <Badge tone="up">{(v.n_sims ?? 0).toLocaleString()} sims</Badge>
                : <span className="ml-auto"><Btn kind="primary" onClick={() => simulate(v.id)}>Simulate</Btn></span>}
              {v.has_sims && v.is_current &&
                <span className="ml-auto"><Btn onClick={() => simulate(v.id)}>Re-simulate</Btn></span>}
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}
