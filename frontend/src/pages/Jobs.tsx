import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, Job } from "../api";
import { Badge, Btn, Progress } from "../ui";

export default function Jobs() {
  const qc = useQueryClient();
  const jobs = useQuery({ queryKey: ["jobs"], queryFn: () => api.get<Job[]>("/api/jobs"), refetchInterval: 2000 });
  const tone = (s: string) => s === "done" ? "up" : s === "failed" ? "down" : s === "running" ? "amber" : "dim";
  return (
    <div className="space-y-3">
      <h1 className="eyebrow">Job monitor</h1>
      <div className="panel divide-y divide-[var(--line)]">
        {jobs.data?.map((j) => (
          <div key={j.id} className="px-4 py-2.5 space-y-1">
            <div className="flex items-center gap-3">
              <span className="num text-[var(--dim)]">#{j.id}</span>
              <span className="font-medium">{j.kind}</span>
              <Badge tone={tone(j.status) as any}>{j.status}</Badge>
              <span className="text-[11px] text-[var(--dim)]">{j.created_at.slice(0, 19)}</span>
              {j.status === "running" && (
                <span className="ml-auto">
                  <Btn kind="danger" onClick={() => api.post(`/api/jobs/${j.id}/cancel`).then(() => qc.invalidateQueries({ queryKey: ["jobs"] }))}>
                    Cancel
                  </Btn>
                </span>
              )}
            </div>
            {j.status === "running" && <Progress value={j.progress} message={j.message} />}
            {j.status === "failed" && <pre className="text-[10px] text-[var(--down)] whitespace-pre-wrap max-h-32 overflow-auto">{j.message}</pre>}
          </div>
        ))}
      </div>
    </div>
  );
}
