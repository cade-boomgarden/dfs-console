import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, SlateSummary, watchJob } from "../api";
import { Badge, Btn, Field, Progress } from "../ui";

/** Best guess at the current NFL season and week.
 *  Week 1 kicks off the Thursday after Labor Day (first Monday in September).
 *  A guess only — the fields are editable, and the wrong week silently yields
 *  the wrong projections, so it is always worth a look before ingesting. */
function guessSeasonWeek(): { season: number; week: number } {
  const now = new Date();
  const season = now.getMonth() >= 7 ? now.getFullYear() : now.getFullYear() - 1;
  const sept = new Date(season, 8, 1);
  const firstMonday = new Date(sept);
  firstMonday.setDate(1 + ((8 - sept.getDay()) % 7));   // first Monday of Sept
  const kickoff = new Date(firstMonday);
  kickoff.setDate(firstMonday.getDate() + 3);           // the Thursday after
  const days = Math.floor((now.getTime() - kickoff.getTime()) / 86_400_000);
  const week = Math.min(18, Math.max(1, Math.floor(days / 7) + 1));
  return { season, week };
}

export default function Slates() {
  const qc = useQueryClient();
  const slates = useQuery({ queryKey: ["slates"], queryFn: () => api.get<SlateSummary[]>("/api/slates") });
  const [job, setJob] = useState<{ progress: number; message: string; status: string } | null>(null);
  const [error, setError] = useState("");
  const guess = guessSeasonWeek();
  const [season, setSeason] = useState<number>(guess.season);
  const [week, setWeek] = useState<number>(guess.week);

  const start = async (body: Record<string, unknown>) => {
    setError("");
    try {
      const { job_id } = await api.post<{ job_id: number }>("/api/slates/ingest", body);
      watchJob(job_id, (j) => {
        setJob(j);
        if (j.status === "failed") setError(j.message);
        if (j.status === "done") qc.invalidateQueries({ queryKey: ["slates"] });
      });
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <div className="space-y-4">
      <h1 className="eyebrow">Slates</h1>

      <section className="panel p-4 space-y-3">
        <div className="flex items-end gap-3 flex-wrap">
          <Field label="Season">
            <input type="number" className="w-24" value={season}
              onChange={(e) => setSeason(Number(e.target.value))} />
          </Field>
          <Field label="Week">
            <input type="number" min={1} max={18} className="w-20" value={week}
              onChange={(e) => setWeek(Number(e.target.value))} />
          </Field>
          <Btn kind="primary"
            disabled={!season || !week || week < 1 || week > 18}
            onClick={() => start({ season, week, label: `${season} wk${week}` })}>
            Ingest live main slate
          </Btn>
          <Btn onClick={() => start({ fixture_dir: "backend/tests/fixtures", label: "fixture" })}>
            Ingest fixture slate
          </Btn>
        </div>
        <div className="text-[11px] text-[var(--dim)] max-w-2xl">
          Season and week are guessed from today's date and are worth checking —
          FantasyPros answers a wrong week with a well-formed payload for the wrong
          week, and an omitted week with season-long totals. The slate itself is
          resolved from the DraftKings lobby; these two only select the projections.
        </div>
        {job && job.status !== "done" && (
          <Progress value={job.progress} message={`${job.status} — ${job.message}`} />
        )}
        {job?.status === "done" && job.message && (
          <div className="text-[11px] text-[var(--ink)]">{job.message}</div>
        )}
        {error && (
          <pre className="text-[11px] text-[var(--ink)] whitespace-pre-wrap max-h-40 overflow-auto
                          border-l-2 border-[var(--line)] pl-2">{error}</pre>
        )}
      </section>

      <div className="panel divide-y divide-[var(--line)]">
        {slates.data?.map((s) => (
          <Link key={s.id} to={`/slate/${s.id}`}
            className="flex items-center gap-4 px-4 py-3 hover:bg-[var(--raised)]">
            <div className="font-bold">{s.name || `Draft group ${s.draft_group_id}`}</div>
            <div className="text-[var(--dim)] num">DG {s.draft_group_id}</div>
            {s.season && s.week && (
              <div className="text-[var(--dim)] num">{s.season} wk{s.week}</div>
            )}
            <div className="text-[var(--dim)] num">{s.game_count} games</div>
            <div className="ml-auto flex gap-2">
              {s.has_sims ? <Badge>sims ready</Badge> : <Badge>no sims</Badge>}
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
