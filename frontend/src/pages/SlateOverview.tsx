import { useState } from "react";

type GameSort = "total" | "price_edge" | "vegas_gap" | "concentration";
const SORTS: { key: GameSort; label: string; asc?: boolean }[] = [
  { key: "total", label: "O/U" }, { key: "price_edge", label: "Price" },
  { key: "vegas_gap", label: "Vegas", asc: true }, { key: "concentration", label: "Conc" },
];
const signed = (v: number | null | undefined) =>
  v === null || v === undefined ? "—" : `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(1)}`;
const pct = (v: number | null | undefined) =>
  v === null || v === undefined ? "—" : `${Math.round(v * 100)}%`;

function priceTip(team: string, t: TeamScore | null) {
  if (!t) return undefined;
  return `${team} core (QB, 2 RB, 3 WR, TE, DST): $${t.core_salary.toLocaleString()} projects ` +
    `${t.core_projection.toFixed(1)}, ${signed(t.price_edge)} pts vs what those salaries buy on this slate`;
}
function vegasTip(team: string, t: TeamScore | null) {
  if (!t || t.vegas_expected === null) return undefined;
  return `${team} core QB/RB/WR/TE projects ${t.offense_projection.toFixed(1)}. ` +
    `An implied total of ${t.implied?.toFixed(1)} averages ${t.vegas_expected.toFixed(1)} DK pts for that group ` +
    `(2022–25), so projections run ${signed(t.vegas_gap)} vs the line`;
}
function concTip(team: string, t: TeamScore | null) {
  if (!t || t.concentration === null) return undefined;
  return `${t.top2.join(" and ")} hold ${pct(t.concentration)} of ${team}'s projected receiving yards`;
}

/** One metric row on a game card: away value, home value, game value.
 *  `better` says which direction is favourable, so emphasis (ink vs dim)
 *  marks the favourable side; null keeps every value in ink. */
function ScoreRow({ label, away, home, game, awayTip, homeTip, better = "high", show = signed }: {
  label: string; away: number | null | undefined; home: number | null | undefined;
  game: number | null; awayTip?: string; homeTip?: string;
  better?: "high" | "low" | null; show?: (v: number | null | undefined) => string;
}) {
  const tone = (v: number | null | undefined) =>
    v === null || v === undefined ? "text-[var(--mute)]"
      : better === null || (better === "high" ? v >= 0 : v <= 0) ? "text-[var(--ink)]" : "text-[var(--dim)]";
  return (
    <div className="grid grid-cols-[1fr_auto_1fr] items-baseline gap-2 num text-[11px]">
      <span className={tone(away)} title={awayTip}>{show(away)}</span>
      <span className="text-[9px] uppercase tracking-wider text-[var(--dim)] font-[family-name:var(--font-ui)]">
        {label} <span className={`num ${tone(game)}`}>{show(game)}</span>
      </span>
      <span className={`text-right ${tone(home)}`} title={homeTip}>{show(home)}</span>
    </div>
  );
}
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { api, watchJob } from "../api";
import { Badge, Btn, num, Progress } from "../ui";

interface TeamScore {
  price_edge: number; core_salary: number; core_projection: number;
  implied: number | null; offense_projection: number; vegas_expected: number | null;
  vegas_gap: number | null; concentration: number | null; top2: string[];
}
interface GameRow {
  id: number; home: string; away: string; total: number | null;
  home_spread: number | null; home_implied: number | null; away_implied: number | null;
  scores: { home: TeamScore | null; away: TeamScore | null;
    price_edge: number | null; vegas_gap: number | null; concentration: number | null };
}
interface Detail {
  id: number; name: string; draft_group_id: number; start_time: string | null;
  vegas_median: number | null;
  games: GameRow[];
  pool_versions: { id: number; label: string; created_at: string; is_current: boolean;
    has_sims: boolean; n_sims: number | null }[];
}

export default function SlateOverview() {
  const { slateId } = useParams();
  const qc = useQueryClient();
  const d = useQuery({ queryKey: ["slate", slateId],
    queryFn: () => api.get<Detail>(`/api/slates/${slateId}`) });
  const [job, setJob] = useState<{ progress: number; message: string; status: string } | null>(null);
  const [sort, setSort] = useState<GameSort>("total");

  const simulate = async (pvId: number) => {
    const { job_id } = await api.post<{ job_id: number }>("/api/slates/simulate", { pool_version_id: pvId });
    watchJob(job_id, (j) => {
      setJob(j);
      if (j.status === "done") qc.invalidateQueries();
    });
  };

  if (!d.data) return null;
  const asc = SORTS.find((o) => o.key === sort)?.asc ?? false;
  const key = (g: GameRow) => (sort === "total" ? g.total : g.scores[sort]) ?? null;
  const games = [...d.data.games].sort((a, b) => {
    const ka = key(a), kb = key(b);
    if (ka === null || kb === null) return ka === null ? (kb === null ? 0 : 1) : -1;   // blanks last
    return asc ? ka - kb : kb - ka;
  });
  const median = d.data.vegas_median;
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-lg font-semibold">{d.data.name}</h1>
        <div className="text-[var(--dim)] text-xs num">DG {d.data.draft_group_id} · {d.data.start_time ?? ""}</div>
      </div>
      {job && job.status !== "done" && <Progress value={job.progress} message={job.message} />}

      <section>
        <div className="flex items-center gap-3 mb-2">
          <h2 className="eyebrow">Games — implied totals</h2>
          <div className="ml-auto flex items-center gap-1">
            <span className="eyebrow mr-1">Sort</span>
            {SORTS.map((o) => (
              <button key={o.key} onClick={() => setSort(o.key)} aria-pressed={sort === o.key}
                className={`px-2 py-0.5 rounded text-[11px] ${sort === o.key ? "bg-[var(--raised)] text-[var(--ink)]" : "text-[var(--dim)] hover:text-[var(--ink)]"}`}>
                {o.label}
              </button>
            ))}
          </div>
        </div>
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
              <div className="mt-2 pt-1.5 border-t hairline space-y-0.5">
                <ScoreRow label="Price" away={g.scores.away?.price_edge} home={g.scores.home?.price_edge}
                  game={g.scores.price_edge}
                  awayTip={priceTip(g.away, g.scores.away)} homeTip={priceTip(g.home, g.scores.home)} />
                <ScoreRow label="Vegas" away={g.scores.away?.vegas_gap} home={g.scores.home?.vegas_gap}
                  game={g.scores.vegas_gap} better="low"
                  awayTip={vegasTip(g.away, g.scores.away)} homeTip={vegasTip(g.home, g.scores.home)} />
                <ScoreRow label="Conc" away={g.scores.away?.concentration} home={g.scores.home?.concentration}
                  game={g.scores.concentration} better={null} show={pct}
                  awayTip={concTip(g.away, g.scores.away)} homeTip={concTip(g.home, g.scores.home)} />
              </div>
            </div>
          ))}
          {games.length === 0 && <div className="text-[var(--dim)]">No games — ingest first.</div>}
        </div>
        {games.length > 0 && (
          <div className="mt-2 text-[11px] text-[var(--dim)] max-w-3xl leading-snug">
            <span className="text-[var(--ink)]">Price</span> (higher = cheaper): points the core (QB, 2 RB, 3 WR, TE, DST)
            projects above what its salaries buy on this slate.{" "}
            <span className="text-[var(--ink)]">Vegas</span> (lower = Vegas likes the team more than the projections do):
            the core QB/RB/WR/TE projection minus what that group averages in DK points at the team's implied total (2022–25)
            {median !== null && <>; slate median {signed(median)}</>}.{" "}
            <span className="text-[var(--ink)]">Conc</span>: the top two pass catchers' share of projected receiving
            yards; higher goes with more correlated QB stacks.{" "}
            Game value: Price and Vegas sum both teams, Conc is the higher side. Emphasis marks the favourable direction.
            Hover a number for the inputs.
          </div>
        )}
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
