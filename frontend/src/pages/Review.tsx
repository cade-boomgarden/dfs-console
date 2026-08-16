import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import { Badge, Btn, money, num } from "../ui";

interface Cand {
  raw_key: string; name: string; team: string; position: string;
  points_ppr: number | null;
}
interface Item {
  id: number; source: string; raw_name: string; raw_team: string;
  raw_position: string; salary: number | null; candidates: Cand[];
}

export default function Review() {
  const qc = useQueryClient();
  const items = useQuery({ queryKey: ["review"], queryFn: () => api.get<Item[]>("/api/review") });
  const [msg, setMsg] = useState("");
  const [needsResim, setNeedsResim] = useState(false);

  const resolve = async (id: number, raw_key: string | null) => {
    const r = await api.post<{ projection: number; needs_resim: boolean }>(
      `/api/review/${id}/resolve`, raw_key ? { raw_key } : { ignore: true });
    if (raw_key) {
      setMsg(`Attached — projection ${num(r.projection)}`);
      if (r.needs_resim) setNeedsResim(true);
    }
    qc.invalidateQueries({ queryKey: ["review"] });
    qc.invalidateQueries({ queryKey: ["pool"] });
  };

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3">
        <h1 className="eyebrow">Projection review</h1>
        {items.data && items.data.length > 0 && <Badge>{items.data.length} open</Badge>}
        {msg && <span className="text-[11px] text-[var(--ink)]">{msg}</span>}
      </div>
      <div className="text-[11px] text-[var(--dim)] max-w-3xl">
        Slate players that no projection resolved to — rookies, mid-week signings,
        source name variants. Each is listed with the unmatched FantasyPros records
        that could plausibly be the same person. Attaching one writes the projection
        onto the current pool immediately and remembers the mapping for future pulls.
      </div>
      {needsResim && (
        <div className="panel px-3 py-2 text-[11px]">
          Projections changed — re-run <span className="text-[var(--ink)]">Simulate</span> on
          the slate overview so floors and ceilings reflect the new lines.
        </div>
      )}

      <div className="panel divide-y divide-[var(--line)]">
        {items.data?.map((it) => (
          <div key={it.id} className="px-4 py-3">
            <div className="flex items-center gap-3">
              <span className="font-bold">{it.raw_name}</span>
              <span className="text-[var(--dim)] num">{it.raw_team} {it.raw_position}</span>
              {it.salary != null && <span className="num text-[var(--dim)]">{money(it.salary)}</span>}
              <span className="ml-auto">
                <Btn kind="ghost" onClick={() => resolve(it.id, null)}>No projection exists</Btn>
              </span>
            </div>
            {it.candidates.length === 0 ? (
              <div className="text-[11px] text-[var(--mute)] mt-1">
                No plausible FantasyPros record on this team — likely genuinely absent
                from the projection set.
              </div>
            ) : (
              <div className="mt-2 space-y-1">
                {it.candidates.map((c) => (
                  <button key={c.raw_key} onClick={() => resolve(it.id, c.raw_key)}
                    className="w-full flex items-center gap-3 px-2 py-1 rounded text-left
                               hover:bg-[var(--raised)] border border-transparent
                               hover:border-[var(--line)]">
                    <span className="flex-1">{c.name}</span>
                    <span className="text-[var(--dim)] num">{c.team} {c.position}</span>
                    <span className="num">{num(c.points_ppr)} pts</span>
                  </button>
                ))}
              </div>
            )}
          </div>
        ))}
        {items.data?.length === 0 && (
          <div className="px-4 py-6 text-[var(--dim)]">
            Every slate player has a projection.
          </div>
        )}
      </div>
    </div>
  );
}
