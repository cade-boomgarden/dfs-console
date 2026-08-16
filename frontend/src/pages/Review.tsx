import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import { Btn } from "../ui";

interface Item {
  id: number; source: string; raw_name: string; raw_team: string;
  raw_position: string; context: Record<string, unknown>; created_at: string;
}
interface Cand { id: number; name: string; team: string; position: string }

export default function Review() {
  const qc = useQueryClient();
  const items = useQuery({ queryKey: ["review"], queryFn: () => api.get<Item[]>("/api/review") });
  const [open, setOpen] = useState<number | null>(null);
  const [q, setQ] = useState("");
  const cands = useQuery({ queryKey: ["cands", q], enabled: open !== null,
    queryFn: () => api.get<Cand[]>(`/api/review/candidates?q=${encodeURIComponent(q)}`) });

  const resolve = async (itemId: number, playerId: number | null) => {
    await api.post(`/api/review/${itemId}/resolve`, { player_id: playerId });
    setOpen(null);
    qc.invalidateQueries({ queryKey: ["review"] });
  };

  return (
    <div className="space-y-3">
      <h1 className="eyebrow">Player match review</h1>
      <div className="text-[11px] text-[var(--dim)]">
        Unmatched source names fail loudly into this queue — rookies, mid-week signings, book name variants.
        Resolving persists the mapping so each variant resolves once.
      </div>
      <div className="panel divide-y divide-[var(--line)]">
        {items.data?.map((it) => (
          <div key={it.id} className="px-4 py-2.5">
            <div className="flex items-center gap-3">
              <span className="font-medium">{it.raw_name}</span>
              <span className="text-[var(--dim)]">{it.raw_team} {it.raw_position}</span>
              <span className="text-[11px] text-[var(--dim)]">from {it.source}</span>
              <span className="ml-auto flex gap-2">
                <Btn onClick={() => { setOpen(open === it.id ? null : it.id); setQ(it.raw_name.split(" ").slice(-1)[0]); }}>
                  Match…
                </Btn>
                <Btn kind="ghost" onClick={() => resolve(it.id, null)}>Ignore</Btn>
              </span>
            </div>
            {open === it.id && (
              <div className="mt-2 space-y-1">
                <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search canonical players" className="w-64" />
                {cands.data?.map((c) => (
                  <button key={c.id} onClick={() => resolve(it.id, c.id)}
                    className="block w-full text-left px-2 py-1 rounded hover:bg-[var(--raised)] text-xs">
                    {c.name} <span className="text-[var(--dim)]">{c.team} {c.position}</span>
                  </button>
                ))}
              </div>
            )}
          </div>
        ))}
        {items.data?.length === 0 && <div className="px-4 py-6 text-[var(--dim)]">Queue is clear.</div>}
      </div>
    </div>
  );
}
