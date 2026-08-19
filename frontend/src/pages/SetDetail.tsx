import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { api, LineupDTO } from "../api";
import { Badge, DistStrip, distDomain, distMaxDensity, money, num } from "../ui";

interface Exposure {
  player_id: number; name: string; position: string; team: string;
  opponent: string; salary: number | null; projection: number | null;
  ceiling: number | null; ownership: number | null; value: number | null;
  implied_total: number | null; count: number; exposure: number;
}
interface TeamExposure {
  team: string; implied_total: number | null; lineups: number;
  lineup_pct: number; slots: number; slots_per_lineup: number;
}
interface Detail {
  id: number; kind: string; label: string; n_eff: number | null; n_eff_flag: boolean;
  lineups: LineupDTO[]; exposures: Exposure[]; team_exposures: TeamExposure[];
  diagnostics: { n_eff_random_baseline?: number; n_candidates?: number;
                 selection_basis?: string; weight_basis?: string };
  overlap_hist: number[]; type_counts: Record<string, number>;
}

const POSITIONS = ["ALL", "QB", "RB", "WR", "TE", "DST"] as const;
type SortKey = "exposure" | "projection" | "value" | "salary" | "ownership";
type LineupSortKey = "ordinal" | "projection" | "ceiling" | "salary" | "ownership"
  | "median" | "ev" | "neff_delta";

function Bar({ pct }: { pct: number }) {
  return (
    <div className="w-14 h-1.5 bg-[var(--raised)] rounded inline-block align-middle">
      <div className="h-full bg-[var(--ink)] rounded" style={{ width: `${Math.min(pct * 100, 100)}%` }} />
    </div>
  );
}

export default function SetDetail() {
  const { slateId, setId } = useParams();
  const d = useQuery({ queryKey: ["set", setId],
    queryFn: () => api.get<Detail>(`/api/slates/${slateId}/sets/${setId}`) });
  const [pos, setPos] = useState<string>("ALL");
  const [sort, setSort] = useState<SortKey>("exposure");
  const [luSort, setLuSort] = useState<LineupSortKey>("ordinal");
  const [luDesc, setLuDesc] = useState(false);
  const [luType, setLuType] = useState("ALL");
  const [luQuery, setLuQuery] = useState("");

  const rows = useMemo(() => {
    const list = (d.data?.exposures ?? []).filter((e) => pos === "ALL" || e.position === pos);
    return [...list].sort((a, b) => (Number(b[sort] ?? -Infinity) - Number(a[sort] ?? -Infinity)));
  }, [d.data, pos, sort]);

  const lineupRows = useMemo(() => {
    const list = (d.data?.lineups ?? []).filter((lu) => {
      if (luType !== "ALL" && lu.lineup_type !== luType) return false;
      if (!luQuery) return true;
      const q = luQuery.toLowerCase();
      return lu.slots.some((sl) => (sl.name ?? "").toLowerCase().includes(q));
    });
    const val = (lu: LineupDTO) =>
      luSort === "median" ? (lu.evaluation.median ?? 0)
        : luSort === "ev" ? (lu.evaluation.expected_payout ?? 0)
        : luSort === "neff_delta" ? (lu.evaluation.neff_delta ?? 0)
        : luSort === "ordinal" ? lu.ordinal
        : (lu[luSort] as number ?? 0);
    return [...list].sort((a, b) => (luDesc ? val(b) - val(a) : val(a) - val(b)));
  }, [d.data, luSort, luDesc, luType, luQuery]);

  const domain = useMemo(() => distDomain(d.data?.lineups ?? []), [d.data]);
  const yMax = useMemo(() => distMaxDensity(d.data?.lineups ?? []), [d.data]);

  if (!d.data) return null;
  const s = d.data;
  const maxOv = Math.max(...s.overlap_hist, 1);
  const baseline = s.diagnostics?.n_eff_random_baseline;
  const hasEv = s.lineups.some((lu) => lu.evaluation.expected_payout != null);
  const hasDelta = s.lineups.some((lu) => lu.evaluation.neff_delta != null);

  const Th = ({ k, children, right }: { k?: SortKey; children: React.ReactNode; right?: boolean }) => (
    <th onClick={k ? () => setSort(k) : undefined}
      className={`px-2 py-1.5 text-[10px] uppercase tracking-wider text-[var(--dim)] whitespace-nowrap
        ${right ? "text-right" : "text-left"} ${k ? "cursor-pointer select-none" : ""}`}>
      {children}{k && sort === k ? " ↓" : ""}
    </th>
  );

  return (
    <div className="space-y-5">
      <div className="flex items-center gap-3 flex-wrap">
        <h1 className="text-lg font-bold">{s.label}</h1>
        <Badge>{s.kind}</Badge>
        <span className="num text-[var(--dim)]">{s.lineups.length} lineups</span>
        {s.n_eff !== null && (
          <span className="num" title="Effective number of independent lineups, from the eigenvalues of the score covariance">
            N<sub>eff</sub> <span className="text-[var(--ink)]">{num(s.n_eff)}</span>
            {baseline != null && (
              <span className="text-[var(--dim)]"> / {num(baseline)} random</span>
            )}
          </span>
        )}
        {s.n_eff_flag && <Badge>below your calibrated floor</Badge>}
        {s.diagnostics?.selection_basis && (
          <span className="text-[10px] text-[var(--dim)]">
            selected by {s.diagnostics.selection_basis === "expected_payout"
              ? "expected payout vs sampled field" : "expected score"}
          </span>
        )}
      </div>

      {/* ---------------- exposure report ---------------- */}
      <section className="grid grid-cols-[1fr_360px] gap-4 items-start">
        <div className="panel">
          <div className="flex items-center gap-2 px-3 py-2 border-b hairline">
            <span className="eyebrow">Player exposure</span>
            <div className="ml-auto flex gap-1">
              {POSITIONS.map((p) => (
                <button key={p} onClick={() => setPos(p)}
                  className={`px-2 py-0.5 rounded text-xs ${pos === p ? "bg-[var(--raised)] text-[var(--ink)]" : "text-[var(--dim)] hover:text-[var(--ink)]"}`}>
                  {p}
                </button>
              ))}
            </div>
          </div>
          <div className="overflow-auto max-h-[62vh]">
            <table className="w-full">
              <thead className="sticky top-0 bg-[var(--panel)]">
                <tr className="border-b hairline">
                  <Th>Player</Th><Th>Pos</Th><Th>Tm</Th>
                  <Th k="salary" right>Salary</Th>
                  <Th k="projection" right>Proj</Th>
                  <Th k="value" right>Val</Th>
                  <Th k="ownership" right>Own%</Th>
                  <Th right>Impl</Th>
                  <Th k="exposure" right>Exposure</Th>
                </tr>
              </thead>
              <tbody>
                {rows.map((e) => (
                  <tr key={e.player_id} className="border-b hairline hover:bg-[var(--raised)]">
                    <td className="px-2 py-1 whitespace-nowrap">{e.name}</td>
                    <td className="px-2 py-1 text-[var(--dim)]">{e.position}</td>
                    <td className="px-2 py-1 text-[var(--dim)]">{e.team}</td>
                    <td className="px-2 py-1 text-right num">{money(e.salary)}</td>
                    <td className="px-2 py-1 text-right num">{num(e.projection)}</td>
                    <td className="px-2 py-1 text-right num">{num(e.value, 2)}</td>
                    <td className="px-2 py-1 text-right num text-[var(--dim)]">{num(e.ownership)}</td>
                    <td className="px-2 py-1 text-right num text-[var(--dim)]">{num(e.implied_total)}</td>
                    <td className="px-2 py-1 text-right num whitespace-nowrap">
                      <Bar pct={e.exposure} />
                      <span className="ml-2">{(e.exposure * 100).toFixed(0)}%</span>
                      <span className="ml-1 text-[var(--mute)]">({e.count})</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="px-3 py-1.5 text-[10px] text-[var(--dim)] border-t hairline">
            {rows.length} players · Val = projected points per $1,000 · Impl = team implied total
          </div>
        </div>

        <div className="space-y-4">
          <div className="panel">
            <div className="px-3 py-2 border-b hairline eyebrow">Team exposure</div>
            <div className="overflow-auto max-h-[38vh]">
              <table className="w-full">
                <thead className="sticky top-0 bg-[var(--panel)]">
                  <tr className="border-b hairline">
                    <Th>Team</Th><Th right>Impl</Th><Th right>Slots/LU</Th><Th right>In lineups</Th>
                  </tr>
                </thead>
                <tbody>
                  {s.team_exposures.map((t) => (
                    <tr key={t.team} className="border-b hairline hover:bg-[var(--raised)]">
                      <td className="px-2 py-1">{t.team}</td>
                      <td className="px-2 py-1 text-right num">{num(t.implied_total)}</td>
                      <td className="px-2 py-1 text-right num">{num(t.slots_per_lineup, 2)}</td>
                      <td className="px-2 py-1 text-right num whitespace-nowrap">
                        <Bar pct={t.lineup_pct} />
                        <span className="ml-2">{(t.lineup_pct * 100).toFixed(0)}%</span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="px-3 py-1.5 text-[10px] text-[var(--dim)] border-t hairline">
              Slots/LU = average players from that team per lineup
            </div>
          </div>

          <div className="panel p-3">
            <div className="eyebrow mb-2">Pairwise overlap</div>
            <div className="flex items-end gap-0.5 h-14">
              {s.overlap_hist.map((c, i) => (
                <div key={i} className="flex-1 flex flex-col items-center gap-0.5">
                  <div className="w-full bg-[var(--chart)]" style={{ height: `${(c / maxOv) * 100}%` }} />
                  <span className="text-[9px] text-[var(--dim)] num">{i}</span>
                </div>
              ))}
            </div>
          </div>

          <div className="panel p-3">
            <div className="eyebrow mb-2">Lineup types</div>
            {Object.entries(s.type_counts).sort((a, b) => b[1] - a[1]).map(([t, c]) => (
              <div key={t} className="flex justify-between text-[11px] num">
                <span className="text-[var(--dim)]">{t}</span><span>{c}</span>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* ---------------- lineups ---------------- */}
      <section className="panel">
        <div className="flex items-center gap-2 px-3 py-2 border-b hairline flex-wrap">
          <span className="eyebrow">Lineups</span>
          <span className="num text-[var(--dim)]">{lineupRows.length} of {s.lineups.length}</span>
          <select value={luType} onChange={(e) => setLuType(e.target.value)} className="ml-auto">
            <option value="ALL">All types</option>
            {Object.keys(s.type_counts).sort().map((t) => (
              <option key={t} value={t}>{t} ({s.type_counts[t]})</option>
            ))}
          </select>
          <input placeholder="Contains player…" value={luQuery}
            onChange={(e) => setLuQuery(e.target.value)} className="w-52" />
        </div>
        <div className="overflow-auto max-h-[60vh]">
        <table className="w-full">
          <thead className="sticky top-0 bg-[var(--panel)]">
            <tr className="border-b hairline">
              {([["ordinal", "#"], [null, `Distribution ${domain ? `(${domain[0].toFixed(0)}–${domain[1].toFixed(0)})` : ""}`], ["median", "Med"],
                 ["projection", "Proj"], ["ceiling", "Ceil"], ["salary", "Sal"],
                 ["ownership", "Own"],
                 ...(hasEv ? [["ev", "EV$"], [null, "ROI"]] : []),
                 ...(hasDelta ? [["neff_delta", "ΔNeff"]] : []),
                 [null, "Type"], [null, "Lineup"]] as
                 [LineupSortKey | null, string][]).map(([k, h]) => (
                <th key={h}
                  onClick={k ? () => { luSort === k ? setLuDesc(!luDesc) : setLuSort(k); } : undefined}
                  className={`px-2 py-1.5 text-left text-[10px] uppercase tracking-wider text-[var(--dim)] ${k ? "cursor-pointer select-none" : ""}`}>
                  {h}{k && luSort === k ? (luDesc ? " ↓" : " ↑") : ""}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {lineupRows.map((lu) => (
              <tr key={lu.id} className="border-b hairline hover:bg-[var(--raised)] align-top">
                <td className="px-2 py-1.5 num text-[var(--dim)]">{lu.ordinal + 1}</td>
                <td className="px-2 py-1.5">
                  <DistStrip histogram={lu.evaluation.histogram} edges={lu.evaluation.hist_edges}
                    floor={lu.evaluation.floor} median={lu.evaluation.median}
                    ceiling={lu.evaluation.ceiling} domain={domain} yMax={yMax} />
                </td>
                <td className="px-2 py-1.5 num">{num(lu.evaluation.median)}</td>
                <td className="px-2 py-1.5 num">{num(lu.projection)}</td>
                <td className="px-2 py-1.5 num">{num(lu.ceiling)}</td>
                <td className="px-2 py-1.5 num">{money(lu.salary)}</td>
                <td className="px-2 py-1.5 num">{num(lu.ownership, 0)}%</td>
                {hasEv && (
                  <>
                    <td className="px-2 py-1.5 num">{num(lu.evaluation.expected_payout, 2)}</td>
                    <td className="px-2 py-1.5 num">
                      {lu.evaluation.roi != null ? `${(lu.evaluation.roi * 100).toFixed(0)}%` : "—"}
                    </td>
                  </>
                )}
                {hasDelta && (
                  <td className="px-2 py-1.5 num"
                    title="Leave-one-out N_eff delta — near zero means this entry adds EV but no new bet">
                    <span className={(lu.evaluation.neff_delta ?? 1) < 0.05 ? "text-[var(--down)]" : ""}>
                      {num(lu.evaluation.neff_delta, 2)}
                    </span>
                  </td>
                )}
                <td className="px-2 py-1.5 text-[11px]">{lu.lineup_type}</td>
                <td className="px-2 py-1.5 text-[11px] text-[var(--dim)] max-w-[420px]">
                  {lu.slots.map((sl) => sl.name).join(" · ")}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      </section>
    </div>
  );
}
