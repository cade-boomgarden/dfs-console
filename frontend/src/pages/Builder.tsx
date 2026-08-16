import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { api, Evaluation, PoolPlayer } from "../api";
import { Btn, DistStrip, Field, money, num } from "../ui";

const SLOTS = ["QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX", "DST"];
const ELIGIBLE: Record<string, string[]> = {
  QB: ["QB"], RB: ["RB"], WR: ["WR"], TE: ["TE"], DST: ["DST"], FLEX: ["RB", "WR", "TE"],
};

export default function Builder() {
  const { slateId } = useParams();
  const pool = useQuery({ queryKey: ["pool", slateId],
    queryFn: () => api.get<{ players: PoolPlayer[]; has_sims: boolean }>(`/api/slates/${slateId}/pool`) });
  const [slots, setSlots] = useState<(number | null)[]>(Array(9).fill(null));
  const [active, setActive] = useState(0);
  const [q, setQ] = useState("");
  const [issues, setIssues] = useState<string[]>([]);
  const [ev, setEv] = useState<Evaluation | null>(null);
  const [busy, setBusy] = useState("");
  const timer = useRef<number>();

  const byId = useMemo(() => {
    const m = new Map<number, PoolPlayer>();
    pool.data?.players.forEach((p) => m.set(p.player_id, p));
    return m;
  }, [pool.data]);

  const picked = slots.filter((s): s is number => s !== null);
  const salary = picked.reduce((a, id) => a + (byId.get(id)?.salary ?? 0), 0);

  // live validate + evaluate, debounced -- the sub-100ms loop (section 12)
  useEffect(() => {
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(async () => {
      if (picked.length === 0) { setIssues([]); setEv(null); return; }
      const v = await api.post<{ issues: string[] }>(`/api/slates/${slateId}/builder/validate`, { player_ids: slots });
      setIssues(v.issues);
      if (pool.data?.has_sims) {
        const e = await api.post<Evaluation>(`/api/slates/${slateId}/builder/evaluate`, { player_ids: slots });
        setEv(e);
      }
    }, 120);
    return () => window.clearTimeout(timer.current);
  }, [slots, slateId, pool.data?.has_sims]);

  const options = useMemo(() => {
    const slotName = SLOTS[active];
    const inUse = new Set(picked);
    return (pool.data?.players ?? [])
      .filter((p) => ELIGIBLE[slotName].includes(p.position) && !inUse.has(p.player_id))
      .filter((p) => !q || p.name.toLowerCase().includes(q.toLowerCase()) || p.team.toLowerCase() === q.toLowerCase())
      .sort((a, b) => b.salary - a.salary)
      .slice(0, 60);
  }, [pool.data, active, q, picked]);

  const pick = (id: number) => {
    const next = [...slots];
    next[active] = id;
    setSlots(next);
    const empty = next.findIndex((s) => s === null);
    if (empty >= 0) setActive(empty);
    setQ("");
  };

  const complete = async () => {
    setBusy("Completing…");
    const r = await api.post<{ lineups: { slot: string; player_id: number }[][] }>(
      `/api/slates/${slateId}/builder/complete`, { player_ids: slots, n: 1 });
    setBusy("");
    if (r.lineups.length) setSlots(r.lineups[0].map((s) => s.player_id));
  };

  const save = async (isDraft: boolean) => {
    setBusy("Saving…");
    await api.post(`/api/slates/${slateId}/builder/save`, { player_ids: slots, is_draft: isDraft });
    setBusy("");
  };

  return (
    <div className="grid grid-cols-[300px_1fr_280px] gap-4">
      {/* slot rail */}
      <div className="space-y-1">
        <h2 className="eyebrow mb-2">Lineup</h2>
        {SLOTS.map((s, i) => {
          const p = slots[i] !== null ? byId.get(slots[i]!) : null;
          return (
            <button key={i} onClick={() => setActive(i)}
              className={`w-full flex items-center gap-2 px-2 py-1.5 rounded border text-left
                ${active === i ? "border-[var(--amber)]" : "hairline border"} bg-[var(--panel)]`}>
              <span className="eyebrow w-9">{s}</span>
              {p ? (
                <>
                  <span className="flex-1 truncate">{p.name}</span>
                  <span className="num text-[var(--dim)]">{money(p.salary)}</span>
                  <span className="text-[var(--dim)] cursor-pointer px-1"
                    onClick={(e) => { e.stopPropagation(); const n = [...slots]; n[i] = null; setSlots(n); setActive(i); }}>✕</span>
                </>
              ) : <span className="text-[var(--dim)]">—</span>}
            </button>
          );
        })}
        <div className="flex justify-between pt-2 num">
          <span className="text-[var(--dim)]">Salary</span>
          <span className={salary > 50000 ? "text-[var(--down)]" : ""}>{money(salary)} / $50,000</span>
        </div>
        <div className="flex gap-2 pt-2">
          <Btn kind="primary" onClick={complete} disabled={!!busy}>Complete with optimizer</Btn>
          <Btn onClick={() => setSlots(Array(9).fill(null))} kind="ghost">Clear</Btn>
        </div>
        <div className="flex gap-2">
          <Btn onClick={() => save(false)} disabled={picked.length < 9 || !!busy}>Save lineup</Btn>
          <Btn onClick={() => save(true)} kind="ghost" disabled={picked.length === 0 || !!busy}>Save draft</Btn>
        </div>
        {busy && <div className="text-[11px] text-[var(--dim)]">{busy}</div>}
        {issues.length > 0 && (
          <ul className="pt-2 space-y-1">
            {issues.map((i, k) => <li key={k} className="text-[11px] text-[var(--down)]">{i}</li>)}
          </ul>
        )}
      </div>

      {/* picker */}
      <div>
        <div className="flex gap-2 items-center mb-2">
          <h2 className="eyebrow">Fill {SLOTS[active]}</h2>
          <input autoFocus placeholder="Type to search…" value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && options[0]) pick(options[0].player_id); }}
            className="ml-auto w-56" />
        </div>
        <div className="panel overflow-auto max-h-[75vh]">
          <table className="w-full">
            <thead className="sticky top-0 bg-[var(--panel)]">
              <tr className="border-b hairline">
                {["Player", "Tm", "Opp", "Salary", "Proj", "Floor", "Ceil", "Own%"].map((h) => (
                  <th key={h} className="px-2 py-1.5 text-left text-[10px] uppercase tracking-wider text-[var(--dim)]">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {options.map((p) => (
                <tr key={p.player_id} onClick={() => pick(p.player_id)}
                  className="border-b hairline hover:bg-[var(--raised)] cursor-pointer">
                  <td className="px-2 py-1">{p.name}{p.status && <span className="ml-1 text-[var(--down)] text-[10px]">{p.status}</span>}</td>
                  <td className="px-2 py-1">{p.team}</td>
                  <td className="px-2 py-1 text-[var(--dim)]">{p.opponent}</td>
                  <td className="px-2 py-1">{money(p.salary)}</td>
                  <td className="px-2 py-1">{num(p.projection)}</td>
                  <td className="px-2 py-1 text-[var(--down)]">{num(p.floor)}</td>
                  <td className="px-2 py-1 text-[var(--up)]">{num(p.ceiling)}</td>
                  <td className="px-2 py-1">{num(p.ownership)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* live evaluation */}
      <div className="space-y-3">
        <h2 className="eyebrow">Live evaluation</h2>
        {!pool.data?.has_sims && <div className="text-[11px] text-[var(--amber)]">Build the sims matrix to evaluate.</div>}
        {ev && (
          <div className="panel p-3 space-y-3">
            <DistStrip histogram={ev.histogram} edges={ev.hist_edges}
              floor={ev.floor} median={ev.median} ceiling={ev.ceiling} width={244} height={56} />
            <div className="grid grid-cols-2 gap-x-4 gap-y-1 num text-xs">
              <span className="text-[var(--dim)]">Mean</span><span>{num(ev.projection)}</span>
              <span className="text-[var(--dim)]">Floor p20</span><span className="text-[var(--down)]">{num(ev.floor)}</span>
              <span className="text-[var(--dim)]">Median</span><span>{num(ev.median)}</span>
              <span className="text-[var(--dim)]">Ceiling p85</span><span className="text-[var(--up)]">{num(ev.ceiling)}</span>
              <span className="text-[var(--dim)]">p95</span><span>{num(ev.p95)}</span>
              <span className="text-[var(--dim)]">Std dev</span><span>{num(ev.stddev)}</span>
              <span className="text-[var(--dim)]">Salary left</span><span>{money(ev.salary_remaining)}</span>
              <span className="text-[var(--dim)]">Cum own</span><span>{num(ev.cumulative_ownership)}%</span>
              {ev.lineup_type && (<><span className="text-[var(--dim)]">Type</span><span>{ev.lineup_type}</span></>)}
            </div>
            {picked.length > 0 && Object.keys(ev.marginal).length > 0 && (
              <div>
                <div className="eyebrow mb-1">Per-player mean</div>
                {Object.entries(ev.marginal).map(([pid, m]) => (
                  <div key={pid} className="flex justify-between text-[11px] num">
                    <span className="text-[var(--dim)] truncate">{byId.get(Number(pid))?.name ?? pid}</span>
                    <span>{num(m)}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
