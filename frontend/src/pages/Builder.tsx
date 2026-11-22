import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { api, Evaluation, PoolPlayer } from "../api";
import { Btn, DistStrip, Field, money, num } from "../ui";
import { PlayerHover, usePoolProfiles } from "../components/PlayerCard";
import { EyeButton, PlayerDrawer } from "../components/PlayerDetail";
import { CopyLineup } from "../components/CopyLineup";
import { CopyRow } from "../lineupCopy";

const CLASSIC_SLOTS = ["QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX", "DST"];
// Showdown: the captain scores 1.5x at 1.5x salary; every position is eligible
const SHOWDOWN_SLOTS = ["CPT", "FLEX", "FLEX", "FLEX", "FLEX", "FLEX"];
const ALL_POS = ["QB", "RB", "WR", "TE", "K", "DST"];
const SALARY_CAP = 50000;
const signedMoney = (v: number) => (v < 0 ? `-${money(-v)}` : money(v));
type Objective = "mean" | "median" | "ceiling";
const OBJECTIVES: { key: Objective; label: string; hint: string }[] = [
  { key: "mean", label: "Mean", hint: "Sum of each player's sim mean" },
  { key: "median", label: "Median", hint: "Sum of each player's sim p50" },
  { key: "ceiling", label: "Ceiling", hint: "Sum of each player's sim p85" },
];
// A 2- or 3-man group from the current slots and how many saved lineups
// in the hand set already contain all of it.
type Group = { slots: number[]; ids: number[]; count: number };
const TOP_GROUPS = 5;

const ELIGIBLE: Record<string, string[]> = {
  QB: ["QB"], RB: ["RB"], WR: ["WR"], TE: ["TE"], DST: ["DST"], FLEX: ["RB", "WR", "TE"],
};
const ELIGIBLE_SD: Record<string, string[]> = { CPT: ALL_POS, FLEX: ALL_POS };

export default function Builder() {
  const { slateId } = useParams();
  const pool = useQuery({ queryKey: ["pool", slateId],
    queryFn: () => api.get<{ players: PoolPlayer[]; has_sims: boolean; showdown?: boolean }>(`/api/slates/${slateId}/pool`) });
  const showdown = Boolean(pool.data?.showdown);
  const SLOTS = showdown ? SHOWDOWN_SLOTS : CLASSIC_SLOTS;
  const eligible = showdown ? ELIGIBLE_SD : ELIGIBLE;
  const isCpt = (i: number) => showdown && i === 0;
  const profiles = usePoolProfiles(slateId);
  // the hand set Builder saves land in; refetched after every save
  const qc = useQueryClient();
  const handSet = useQuery({ queryKey: ["builder-set", slateId],
    queryFn: () => api.get<{ lineup_set_id: number | null; label: string; n_drafts: number; lineups: number[][] }>(
      `/api/slates/${slateId}/builder/set`) });
  const [hoverGroup, setHoverGroup] = useState<number[] | null>(null);
  const [slots, setSlots] = useState<(number | null)[]>(Array(9).fill(null));
  const [active, setActive] = useState(0);
  const [q, setQ] = useState("");
  const [issues, setIssues] = useState<string[]>([]);
  const [ev, setEv] = useState<Evaluation | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  // Fades: players the optimizer must skip when completing. Two sources:
  // Pool-page excludes (saved on the slate, shown here as fades) and
  // Builder-session fades (survive Clear so you can hand-build several
  // lineups against the same list, gone when you leave the page).
  const [fades, setFades] = useState<Set<number>>(new Set());
  const poolFades = useMemo(() => new Set((pool.data?.players ?? [])
    .filter((p) => p.adjustments.exclude).map((p) => p.player_id)), [pool.data]);
  const allFades = useMemo(() => new Set([...poolFades, ...fades]), [poolFades, fades]);
  const [objective, setObjective] = useState<Objective>("mean");
  const [detail, setDetail] = useState<number | null>(null);
  const timer = useRef<number>();
  // the slot count follows the slate type once the pool loads
  useEffect(() => {
    setSlots((prev) => (prev.length === SLOTS.length ? prev : Array(SLOTS.length).fill(null)));
    setActive(0);
  }, [SLOTS.length]);
  /** A pool row as it plays in slot i: captain salary/points in the CPT slot. */
  const asSlot = (p: PoolPlayer, i: number) => (isCpt(i) ? {
    ...p, salary: p.cpt_salary ?? Math.round(p.salary * 1.5),
    projection: p.projection * 1.5, floor: p.floor * 1.5, ceiling: p.ceiling * 1.5,
    ownership: p.cpt_ownership ?? 0,
  } : p);
  const byId = useMemo(() => {
    const m = new Map<number, PoolPlayer>();
    pool.data?.players.forEach((p) => m.set(p.player_id, p));
    return m;
  }, [pool.data]);

  const saved = useMemo(() => handSet.data?.lineups ?? [], [handSet.data]);
  useEffect(() => setHoverGroup(null), [slots]);   // hovered row may vanish
  const nSaved = saved.length;
  const expCount = useMemo(() => {
    const m = new Map<number, number>();
    saved.forEach((lu) => lu.forEach((id) => m.set(id, (m.get(id) ?? 0) + 1)));
    return m;
  }, [saved]);
  const expPct = (id: number) => (nSaved ? ((expCount.get(id) ?? 0) / nSaved) * 100 : null);

  // every pair and triple among the filled slots, counted against saved lineups
  const groups = useMemo(() => {
    const sets = saved.map((lu) => new Set(lu));
    const filled = slots.flatMap((id, i) => (id === null ? [] : [[i, id] as const]));
    const count = (ids: number[]) => sets.reduce((a, s) => a + (ids.every((i) => s.has(i)) ? 1 : 0), 0);
    const out: Group[] = [];
    const add = (members: (readonly [number, number])[]) => {
      const ids = members.map(([, id]) => id);
      const c = count(ids);
      if (c > 0) out.push({ slots: members.map(([i]) => i), ids, count: c });
    };
    for (let a = 0; a < filled.length; a++)
      for (let b = a + 1; b < filled.length; b++) {
        add([filled[a], filled[b]]);
        for (let c = b + 1; c < filled.length; c++) add([filled[a], filled[b], filled[c]]);
      }
    out.sort((x, y) => y.count - x.count);
    return {
      triples: out.filter((g) => g.ids.length === 3).slice(0, TOP_GROUPS),
      pairs: out.filter((g) => g.ids.length === 2).slice(0, TOP_GROUPS),
    };
  }, [saved, slots]);
  // hovering a group shows its slots; otherwise the most repeated triple
  const highlighted = new Set(hoverGroup ?? groups.triples[0]?.slots ?? []);

  const picked = slots.filter((s): s is number => s !== null);
  const salary = slots.reduce<number>((a, id, i) => {
    const p = id === null ? undefined : byId.get(id);
    return a + (p ? asSlot(p, i).salary : 0);
  }, 0);
  // the lineup as the copy button formats it: slot-adjusted (CPT 1.5x)
  const copyRows: CopyRow[] = SLOTS.map((s, i) => {
    const raw = slots[i] !== null ? byId.get(slots[i]!) : undefined;
    if (!raw) return { slot: s, name: null, salary: 0, projection: 0, ownership: 0 };
    const p = asSlot(raw, i);
    return { slot: s, name: p.name, salary: p.salary, projection: p.projection, ownership: p.ownership };
  });
  const remaining = SALARY_CAP - salary;
  const openSlots = SLOTS.length - picked.length;
  // floored: the average you can spend on each open slot and still fit
  const perSlot = openSlots > 0 ? Math.floor(remaining / openSlots) : null;

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
    const slotName = SLOTS[active] ?? SLOTS[0];
    const inUse = new Set(picked);
    return (pool.data?.players ?? [])
      .filter((p) => eligible[slotName].includes(p.position) && !inUse.has(p.player_id))
      .filter((p) => !q || p.name.toLowerCase().includes(q.toLowerCase()) || p.team.toLowerCase() === q.toLowerCase())
      .map((p) => asSlot(p, active))
      .sort((a, b) => b.salary - a.salary);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pool.data, active, q, picked, showdown]);

  const pick = (id: number) => {
    const next = [...slots];
    next[active] = id;
    setSlots(next);
    const empty = next.findIndex((s) => s === null);
    if (empty >= 0) setActive(empty);
    setQ("");
    // picking a player by hand overrides a session fade on them; a slotted
    // player is locked, so a Pool exclude does not block the pick either
    if (fades.has(id)) toggleFade(id);
  };

  const toggleFade = async (id: number) => {
    // unfading a Pool exclude removes it from the slate, so Pool agrees
    if (poolFades.has(id)) {
      await api.del(`/api/slates/${slateId}/pool/adjustments/${id}/exclude`);
      qc.invalidateQueries({ queryKey: ["pool", slateId] });
      return;
    }
    setFades((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };

  // fade a slotted player: open the slot and keep the optimizer off them
  const fadeSlot = (i: number) => {
    const id = slots[i];
    if (id === null) return;
    const n = [...slots]; n[i] = null; setSlots(n); setActive(i);
    setFades((prev) => new Set(prev).add(id));
  };

  const complete = async () => {
    setBusy("Completing…");
    setError("");
    const r = await api.post<{ lineups: { slot: string; player_id: number }[][]; error?: string }>(
      `/api/slates/${slateId}/builder/complete`,
      { player_ids: slots, n: 1, fade_ids: [...allFades], objective });
    setBusy("");
    if (r.lineups.length) setSlots(r.lineups[0].map((s) => s.player_id));
    else setError(r.error ?? "No completion found.");
  };

  const save = async (isDraft: boolean) => {
    setBusy("Saving…");
    await api.post(`/api/slates/${slateId}/builder/save`, { player_ids: slots, is_draft: isDraft });
    await qc.invalidateQueries({ queryKey: ["builder-set", slateId] });
    setBusy("");
  };

  return (
    <div className="grid grid-cols-[300px_1fr_280px] gap-4">
      {/* slot rail */}
      <div className="space-y-1">
        <div className="flex items-center justify-between mb-2">
          <h2 className="eyebrow">Lineup</h2>
          <CopyLineup rows={copyRows} disabled={picked.length === 0} />
        </div>
        {SLOTS.map((s, i) => {
          const raw = slots[i] !== null ? byId.get(slots[i]!) : null;
          const p = raw ? asSlot(raw, i) : null;
          return (
            <button key={i} onClick={() => setActive(i)}
              className={`w-full flex items-center gap-2 px-2 py-1.5 rounded border text-left
                ${active === i ? "border-[var(--amber)]" : "hairline border"}
                ${highlighted.has(i) ? "bg-[var(--raised)] shadow-[inset_3px_0_0_var(--ink)]" : "bg-[var(--panel)]"}`}>
              <span className="eyebrow w-9">{s}</span>
              {p ? (
                <>
                  <EyeButton asSpan onClick={() => setDetail(p.player_id)} />
                  <PlayerHover player={p} profile={profiles[p.player_id]} className="flex-1 truncate">{p.name}</PlayerHover>
                  <span className="num text-[var(--dim)]">{money(p.salary)}</span>
                  <span title="Fade: remove and keep the optimizer off this player"
                    className="text-[10px] uppercase tracking-wider text-[var(--dim)] hover:text-[var(--ink)] cursor-pointer px-1"
                    onClick={(e) => { e.stopPropagation(); fadeSlot(i); }}>Fade</span>
                  <span title="Remove" className="text-[var(--dim)] cursor-pointer px-1"
                    onClick={(e) => { e.stopPropagation(); const n = [...slots]; n[i] = null; setSlots(n); setActive(i); }}>✕</span>
                </>
              ) : <span className="text-[var(--dim)]">—</span>}
            </button>
          );
        })}
        <div className="flex justify-between pt-2 num">
          <span className="text-[var(--dim)]">Salary</span>
          <span className={salary > SALARY_CAP ? "text-[var(--down)]" : ""}>{money(salary)} / {money(SALARY_CAP)}</span>
        </div>
        <div className="flex justify-between num">
          <span className="text-[var(--dim)]">Remaining</span>
          <span className={remaining < 0 ? "text-[var(--down)]" : ""}>{signedMoney(remaining)}</span>
        </div>
        <div className="flex justify-between num">
          <span className="text-[var(--dim)]">Per open slot{openSlots > 0 ? ` (${openSlots})` : ""}</span>
          <span className={perSlot !== null && perSlot < 0 ? "text-[var(--down)]" : ""}>
            {perSlot === null ? "—" : signedMoney(perSlot)}
          </span>
        </div>
        <div className="flex items-center gap-2 pt-2">
          <span className="eyebrow">Optimize for</span>
          <div className="flex ml-auto rounded border hairline overflow-hidden">
            {OBJECTIVES.map((o) => {
              const needsSims = o.key !== "mean" && !pool.data?.has_sims;
              const on = objective === o.key;
              return (
                <button key={o.key} disabled={needsSims} aria-pressed={on}
                  title={needsSims ? "Build the sims matrix first" : o.hint}
                  onClick={() => setObjective(o.key)}
                  className={`px-2 py-0.5 text-[11px] disabled:opacity-40
                    ${on ? "bg-[var(--raised)] text-[var(--ink)]" : "text-[var(--dim)] hover:text-[var(--ink)]"}`}>
                  {o.label}
                </button>
              );
            })}
          </div>
        </div>
        <div className="flex gap-2">
          <Btn kind="primary" onClick={complete} disabled={!!busy}>Complete with optimizer</Btn>
          <Btn onClick={() => setSlots(Array(SLOTS.length).fill(null))} kind="ghost">Clear</Btn>
        </div>
        <div className="flex gap-2">
          <Btn onClick={() => save(false)} disabled={picked.length < SLOTS.length || !!busy}>Save lineup</Btn>
          <Btn onClick={() => save(true)} kind="ghost" disabled={picked.length === 0 || !!busy}>Save draft</Btn>
        </div>
        {busy && <div className="text-[11px] text-[var(--dim)]">{busy}</div>}
        {error && <div className="text-[11px] text-[var(--down)]">{error}</div>}
        <div className="pt-2">
          <div className="flex items-center justify-between mb-1">
            <span className="eyebrow">Repeated groups</span>
            <span className="text-[11px] text-[var(--dim)] num"
              title={handSet.data?.n_drafts ? `${handSet.data.n_drafts} draft(s) not counted` : undefined}>
              {nSaved} saved
            </span>
          </div>
          {nSaved === 0
            ? <div className="text-[11px] text-[var(--dim)]">No saved lineups in this set yet.</div>
            : groups.triples.length + groups.pairs.length === 0
              ? <div className="text-[11px] text-[var(--dim)]">No two of these players share a saved lineup.</div>
              : ([["3-man", groups.triples], ["2-man", groups.pairs]] as const).map(([label, list]) =>
                  list.length > 0 && (
                    <div key={label} className="mb-1">
                      <div className="text-[10px] uppercase tracking-wider text-[var(--mute)]">{label}</div>
                      {list.map((g) => {
                        const names = g.ids.map((id) => byId.get(id)?.name ?? id).join(" · ");
                        return (
                          <div key={g.ids.join("-")} title={names}
                            onMouseEnter={() => setHoverGroup(g.slots)} onMouseLeave={() => setHoverGroup(null)}
                            className="flex justify-between gap-2 text-[11px] px-1 rounded hover:bg-[var(--raised)] cursor-default">
                            <span className="truncate">{names}</span>
                            <span className="num shrink-0">{g.count} <span className="text-[var(--dim)]">({num((g.count / nSaved) * 100, 0)}%)</span></span>
                          </div>
                        );
                      })}
                    </div>
                  ))}
        </div>
        <div className="pt-2">
          <div className="flex items-center justify-between mb-1">
            <span className="eyebrow">Fades ({allFades.size})</span>
            {fades.size > 0 && (
              <button className="text-[11px] text-[var(--dim)] hover:text-[var(--ink)]"
                title="Clears Builder fades. Pool excludes stay."
                onClick={() => setFades(new Set())}>Clear Builder fades</button>
            )}
          </div>
          {allFades.size === 0
            ? <div className="text-[11px] text-[var(--dim)]">Fade players from the table or a slot, or exclude them in Pool. The optimizer skips them.</div>
            : (
              <div className="flex flex-wrap gap-1">
                {[...allFades].map((id) => {
                  const fromPool = poolFades.has(id);
                  return (
                  <button key={id} onClick={() => toggleFade(id)}
                    title={fromPool ? "Excluded in Pool. Click to remove the Pool exclude." : "Unfade"}
                    className="flex items-center gap-1 px-1.5 py-0.5 rounded hairline border text-[11px] bg-[var(--panel)] hover:border-[var(--ink)]">
                    <span className="line-through text-[var(--dim)]">{byId.get(id)?.name ?? id}</span>
                    {fromPool && <span className="text-[9px] uppercase tracking-wider text-[var(--mute)]">Pool</span>}
                    <span className="text-[var(--dim)]">✕</span>
                  </button>
                  );
                })}
              </div>
            )}
        </div>
        {issues.length > 0 && (
          <ul className="pt-2 space-y-1">
            {issues.map((i, k) => <li key={k} className="text-[11px] text-[var(--down)]">{i}</li>)}
          </ul>
        )}
      </div>

      {/* picker */}
      <div>
        <div className="flex gap-2 items-center mb-2">
          <h2 className="eyebrow">Fill {SLOTS[active]}{isCpt(active) ? " (1.5x salary and points)" : ""}</h2>
          <input autoFocus placeholder="Type to search…" value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && options[0]) pick(options[0].player_id); }}
            className="ml-auto w-56" />
        </div>
        <div className="panel overflow-auto max-h-[75vh]">
          <table className="w-full">
            <thead className="sticky top-0 bg-[var(--panel)]">
              <tr className="border-b hairline">
                {["", "", "Player", "Pos", "Tm", "Opp", "Salary", "Proj", "Floor", "Ceil",
                  isCpt(active) ? "CPT own%" : "Own%", "Exp%"].map((h) => (
                  <th key={h} className="px-2 py-1.5 text-left text-[10px] uppercase tracking-wider text-[var(--dim)]">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {options.map((p) => {
                const faded = allFades.has(p.player_id);
                const fromPool = poolFades.has(p.player_id);
                return (
                <tr key={p.player_id} onClick={() => pick(p.player_id)}
                  className={`border-b hairline hover:bg-[var(--raised)] cursor-pointer ${faded ? "[&>td:nth-child(n+3)]:opacity-40" : ""}`}>
                  <td className="pl-2 py-1 w-6">
                    <EyeButton onClick={() => setDetail(p.player_id)} />
                  </td>
                  <td className="px-2 py-1 w-12">
                    <button onClick={(e) => { e.stopPropagation(); toggleFade(p.player_id); }}
                      title={fromPool ? "Excluded in Pool. Click to remove the Pool exclude."
                        : faded ? "Unfade" : "Fade: the optimizer skips this player"}
                      className={`text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded border
                        ${faded ? "border-[var(--ink)] text-[var(--ink)]" : "hairline text-[var(--dim)] hover:text-[var(--ink)]"}`}>
                      {faded ? "Faded" : "Fade"}
                    </button>
                  </td>
                  <td className={`px-2 py-1 ${faded ? "line-through" : ""}`}>
                    <PlayerHover player={p} profile={profiles[p.player_id]}>
                      {p.name}{p.status && <span className="ml-1 text-[var(--down)] text-[10px]">{p.status}</span>}
                    </PlayerHover>
                  </td>
                  <td className="px-2 py-1 text-[var(--dim)]">{p.position}</td>
                  <td className="px-2 py-1">{p.team}</td>
                  <td className="px-2 py-1 text-[var(--dim)]">{p.opponent}</td>
                  <td className="px-2 py-1">{money(p.salary)}</td>
                  <td className="px-2 py-1">{num(p.projection)}</td>
                  <td className="px-2 py-1 text-[var(--down)]">{num(p.floor)}</td>
                  <td className="px-2 py-1 text-[var(--up)]">{num(p.ceiling)}</td>
                  <td className="px-2 py-1">{num(p.ownership)}</td>
                  <td className={`px-2 py-1 ${expCount.has(p.player_id) ? "" : "text-[var(--mute)]"}`}>{num(expPct(p.player_id))}</td>
                </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      <PlayerDrawer playerId={detail} slateId={slateId} onClose={() => setDetail(null)} />

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
                {/* slot order, matching the rail and the copy. Iterating
                    ev.marginal directly sorts by player id: JS orders
                    integer-like object keys ascending. */}
                {slots.map((id, i) => {
                  if (id === null) return null;
                  const m = ev.marginal[isCpt(i) ? `c${id}` : String(id)];
                  if (m === undefined) return null;
                  return (
                    <div key={i} className="flex justify-between gap-2 text-[11px] num">
                      <span className="text-[var(--mute)] w-9 shrink-0">{SLOTS[i]}</span>
                      <span className="text-[var(--dim)] truncate flex-1">{byId.get(id)?.name ?? id}</span>
                      <span>{num(m)}</span>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
