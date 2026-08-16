import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { api, LineupDTO } from "../api";
import { Badge, DistStrip, money, num } from "../ui";

interface Detail {
  id: number; kind: string; label: string; n_eff: number | null; n_eff_flag: boolean;
  lineups: LineupDTO[];
  exposures: { player_id: string; name: string; count: number; exposure: number }[];
  overlap_hist: number[]; type_counts: Record<string, number>;
}

export default function SetDetail() {
  const { slateId, setId } = useParams();
  const d = useQuery({ queryKey: ["set", setId],
    queryFn: () => api.get<Detail>(`/api/slates/${slateId}/sets/${setId}`) });
  if (!d.data) return null;
  const s = d.data;
  const maxOv = Math.max(...s.overlap_hist, 1);
  return (
    <div className="space-y-5">
      <div className="flex items-center gap-3">
        <h1 className="text-lg font-semibold">{s.label}</h1>
        <Badge>{s.kind}</Badge>
        {s.n_eff !== null && (
          <span className="num">N<sub>eff</sub>{" "}
            <span className={s.n_eff_flag ? "text-[var(--down)]" : "text-[var(--up)]"}>{num(s.n_eff)}</span>
          </span>
        )}
        {s.n_eff_flag && <Badge tone="down">Stage A producing lookalikes — widen skeleton spread</Badge>}
      </div>

      <div className="grid grid-cols-[1fr_280px] gap-4">
        <div className="panel overflow-auto max-h-[70vh]">
          <table className="w-full">
            <thead className="sticky top-0 bg-[var(--panel)]">
              <tr className="border-b hairline">
                {["#", "Distribution", "Proj", "Ceil", "Sal", "Own", "Type", "Lineup"].map((h) => (
                  <th key={h} className="px-2 py-1.5 text-left text-[10px] uppercase tracking-wider text-[var(--dim)]">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {s.lineups.map((lu) => (
                <tr key={lu.id} className="border-b hairline hover:bg-[var(--raised)] align-top">
                  <td className="px-2 py-1.5 num text-[var(--dim)]">{lu.ordinal + 1}</td>
                  <td className="px-2 py-1.5">
                    <DistStrip histogram={lu.evaluation.histogram} edges={lu.evaluation.hist_edges}
                      floor={lu.evaluation.floor} median={lu.evaluation.median} ceiling={lu.evaluation.ceiling} />
                  </td>
                  <td className="px-2 py-1.5 num">{num(lu.projection)}</td>
                  <td className="px-2 py-1.5 num text-[var(--up)]">{num(lu.ceiling)}</td>
                  <td className="px-2 py-1.5 num">{money(lu.salary)}</td>
                  <td className="px-2 py-1.5 num">{num(lu.ownership, 0)}%</td>
                  <td className="px-2 py-1.5 text-[11px]">{lu.lineup_type}</td>
                  <td className="px-2 py-1.5 text-[11px] text-[var(--dim)] max-w-[420px]">
                    {lu.slots.map((sl) => sl.name).join(" · ")}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="space-y-4">
          <div className="panel p-3">
            <div className="eyebrow mb-2">Pairwise overlap</div>
            <div className="flex items-end gap-0.5 h-16">
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
          <div className="panel p-3 max-h-72 overflow-auto">
            <div className="eyebrow mb-2">Exposure</div>
            {s.exposures.slice(0, 40).map((e) => (
              <div key={e.player_id} className="flex items-center gap-2 text-[11px] py-0.5">
                <span className="flex-1 truncate">{e.name}</span>
                <div className="w-16 h-1.5 bg-[var(--raised)] rounded">
                  <div className="h-full bg-[var(--amber)] rounded" style={{ width: `${e.exposure * 100}%` }} />
                </div>
                <span className="num w-10 text-right">{(e.exposure * 100).toFixed(0)}%</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
