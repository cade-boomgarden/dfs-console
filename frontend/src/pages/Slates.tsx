import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, SlateSummary, watchJob } from "../api";
import { Badge, Btn, Progress } from "../ui";

export default function Slates() {
  const qc = useQueryClient();
  const slates = useQuery({ queryKey: ["slates"], queryFn: () => api.get<SlateSummary[]>("/api/slates") });
  const [job, setJob] = useState<{ progress: number; message: string; status: string } | null>(null);

  const ingest = async (fixture: boolean) => {
    const { job_id } = await api.post<{ job_id: number }>("/api/slates/ingest",
      fixture ? { fixture_dir: "backend/tests/fixtures", label: "fixture" } : {});
    watchJob(job_id, (j) => {
      setJob(j);
      if (j.status === "done") qc.invalidateQueries({ queryKey: ["slates"] });
    });
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <h1 className="eyebrow">Slates</h1>
        <div className="ml-auto flex gap-2">
          <Btn onClick={() => ingest(true)}>Ingest fixture slate</Btn>
          <Btn kind="primary" onClick={() => ingest(false)}>Ingest live main slate</Btn>
        </div>
      </div>
      {job && job.status !== "done" && <Progress value={job.progress} message={`${job.status} — ${job.message}`} />}
      <div className="panel divide-y divide-[var(--line)]">
        {slates.data?.map((s) => (
          <Link key={s.id} to={`/slate/${s.id}`}
            className="flex items-center gap-4 px-4 py-3 hover:bg-[var(--raised)]">
            <div className="font-medium">{s.name || `Draft group ${s.draft_group_id}`}</div>
            <div className="text-[var(--dim)] num">DG {s.draft_group_id}</div>
            <div className="text-[var(--dim)] num">{s.game_count} games</div>
            <div className="ml-auto flex gap-2">
              {s.has_sims ? <Badge tone="up">sims ready</Badge> : <Badge>no sims</Badge>}
            </div>
          </Link>
        ))}
        {slates.data?.length === 0 && (
          <div className="px-4 py-8 text-[var(--dim)]">No slates yet. Ingest one to begin.</div>
        )}
      </div>
    </div>
  );
}
