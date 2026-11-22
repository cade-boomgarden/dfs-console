import { useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import {
  createColumnHelper, flexRender, getCoreRowModel,
  getSortedRowModel, SortingState, useReactTable,
} from "@tanstack/react-table";
import { api, PoolPlayer } from "../api";
import { Btn, money, num } from "../ui";
import { PlayerHover, usePoolProfiles } from "../components/PlayerCard";
import { EyeButton, PlayerDrawer } from "../components/PlayerDetail";

const col = createColumnHelper<PoolPlayer>();

type NumField = "salary" | "projection" | "floor" | "ceiling" | "value" | "ownership"
  | "cpt_ownership" | "dvp_rank" | "implied_opp_total";
type Op = "<" | "<=" | ">" | ">=";
interface Rule { field: NumField; op: Op; value: string }
interface SavedView { name: string; pos: string; rules: Rule[] }

const FIELDS: { key: NumField; label: string; showdownOnly?: boolean }[] = [
  { key: "projection", label: "Proj" }, { key: "value", label: "Val" },
  { key: "salary", label: "Salary" }, { key: "ownership", label: "Own%" },
  { key: "ceiling", label: "Ceil" }, { key: "floor", label: "Floor" },
  { key: "dvp_rank", label: "DvP" }, { key: "implied_opp_total", label: "Opp implied" },
  { key: "cpt_ownership", label: "CPT own%", showdownOnly: true },
];
const fieldLabel = (k: NumField) => FIELDS.find((f) => f.key === k)?.label ?? k;
const VIEWS_KEY = "dfs.pool.views";

function loadViews(): SavedView[] {
  try { return JSON.parse(localStorage.getItem(VIEWS_KEY) ?? "[]"); } catch { return []; }
}
function storeViews(v: SavedView[]) {
  try { localStorage.setItem(VIEWS_KEY, JSON.stringify(v)); } catch { /* storage off */ }
}

function passes(p: PoolPlayer, r: Rule): boolean {
  if (r.value.trim() === "") return true;
  const v = p[r.field] as number | null, t = Number(r.value);
  if (v === null || v === undefined || Number.isNaN(t)) return false;
  return r.op === "<" ? v < t : r.op === "<=" ? v <= t : r.op === ">" ? v > t : v >= t;
}

export default function Pool() {
  const { slateId } = useParams();
  const qc = useQueryClient();
  const pool = useQuery({ queryKey: ["pool", slateId],
    queryFn: () => api.get<{ players: PoolPlayer[]; has_sims: boolean; showdown?: boolean }>(`/api/slates/${slateId}/pool`) });
  const showdown = Boolean(pool.data?.showdown);
  const profiles = usePoolProfiles(slateId);
  const [pos, setPos] = useState("ALL");
  const [q, setQ] = useState("");
  const [sorting, setSorting] = useState<SortingState>([{ id: "salary", desc: true }]);
  const [detail, setDetail] = useState<number | null>(null);
  const [teams, setTeams] = useState<Set<string>>(new Set());
  const [rules, setRules] = useState<Rule[]>([]);
  const [show, setShow] = useState<"all" | "active" | "excluded">("all");
  const [views, setViews] = useState<SavedView[]>(loadViews);
  const [busy, setBusy] = useState(false);
  // Shift+click range: the last checkbox clicked per kind, and the visible row order.
  const anchor = useRef<{ kind: string; id: number } | null>(null);
  const order = useRef<number[]>([]);

  const setAdj = async (playerId: number, kind: string, value: number | null, on: boolean) => {
    if (on) await api.post(`/api/slates/${slateId}/pool/adjustments`, { player_id: playerId, kind, value });
    else await api.del(`/api/slates/${slateId}/pool/adjustments/${playerId}/${kind}`);
    qc.invalidateQueries({ queryKey: ["pool", slateId] });
  };

  const setBulk = async (ids: number[], kind: string, on: boolean, note = "") => {
    if (!ids.length) return;
    setBusy(true);
    try {
      await api.post(`/api/slates/${slateId}/pool/adjustments/bulk`, { player_ids: ids, kind, on, note });
    } finally {
      setBusy(false);
      qc.invalidateQueries({ queryKey: ["pool", slateId] });
    }
  };

  /** Checkbox click. With Shift held, every visible row between the last
   *  clicked box in this column and this one takes this box's new state. */
  const onBox = (kind: string, id: number, on: boolean, shift: boolean) => {
    const a = anchor.current;
    anchor.current = { kind, id };
    if (shift && a && a.kind === kind && a.id !== id) {
      const i = order.current.indexOf(a.id), j = order.current.indexOf(id);
      if (i >= 0 && j >= 0) {
        const ids = order.current.slice(Math.min(i, j), Math.max(i, j) + 1);
        setBulk(ids, kind, on, on ? "shift-click range" : "");
        return;
      }
    }
    setAdj(id, kind, null, on);
  };

  const columns = useMemo(() => [
    col.display({ id: "view", header: "", cell: ({ row }) => (
      <EyeButton onClick={() => setDetail(row.original.player_id)} />
    )}),
    col.display({ id: "lock", header: "🔒", cell: ({ row }) => {
      const a = row.original.adjustments;
      return <input type="checkbox" checked={!!a.lock} title="Lock (Shift+click for a range)"
        onChange={() => {}} onMouseDown={(e) => e.shiftKey && e.preventDefault()}
        onClick={(e) => onBox("lock", row.original.player_id, !a.lock, e.shiftKey)} />;
    }}),
    col.display({ id: "excl", header: "✕", cell: ({ row }) => {
      const a = row.original.adjustments;
      return <input type="checkbox" checked={!!a.exclude} title="Exclude (Shift+click for a range)"
        onChange={() => {}} onMouseDown={(e) => e.shiftKey && e.preventDefault()}
        onClick={(e) => onBox("exclude", row.original.player_id, !a.exclude, e.shiftKey)} />;
    }}),
    col.accessor("name", { header: "Player", cell: (c) => (
      <PlayerHover player={c.row.original} profile={profiles[c.row.original.player_id]}
        className={`cursor-help ${c.row.original.adjustments.exclude ? "line-through text-[var(--dim)]" : ""}`}>
        {c.getValue()}
        {c.row.original.status && <span className="ml-1 text-[var(--down)] text-[10px]">{c.row.original.status}</span>}
      </PlayerHover>
    )}),
    col.accessor("position", { header: "Pos" }),
    col.accessor("team", { header: "Tm" }),
    col.accessor("opponent", { header: "Opp" }),
    col.accessor("salary", { header: "Salary", cell: (c) => money(c.getValue()) }),
    col.accessor("projection", { header: "Proj", cell: (c) => num(c.getValue()) }),
    col.accessor("floor", { header: "Floor", cell: (c) => <span className="text-[var(--down)]">{num(c.getValue())}</span> }),
    col.accessor("ceiling", { header: "Ceil", cell: (c) => <span className="text-[var(--up)]">{num(c.getValue())}</span> }),
    col.accessor("value", { header: "Val", cell: (c) => num(c.getValue(), 2) }),
    col.accessor("ownership", { header: showdown ? "FLEX own%" : "Own%", cell: (c) => num(c.getValue()) }),
    ...(showdown ? [
      col.accessor("cpt_salary", { header: "CPT $", cell: (c) => money(c.getValue() ?? 0) }),
      col.accessor("cpt_ownership", { header: "CPT own%", cell: (c) => num(c.getValue()) }),
      col.display({ id: "own_set", header: "FLEX own set", cell: ({ row }) => (
        <input type="number" step={0.5} className="w-14 px-1 py-0.5 text-right"
          title="Override FLEX ownership; the ownership job keeps it and spreads the rest"
          defaultValue={(row.original.adjustments.ownership as number) ?? ""}
          onBlur={(e) => {
            const v = e.target.value === "" ? null : Number(e.target.value);
            setAdj(row.original.player_id, "ownership", v, v !== null);
          }} />
      )}),
      col.display({ id: "cpt_own_set", header: "CPT own set", cell: ({ row }) => (
        <input type="number" step={0.5} className="w-14 px-1 py-0.5 text-right"
          title="Override captain ownership; the ownership job keeps it and spreads the rest"
          defaultValue={(row.original.adjustments.cpt_ownership as number) ?? ""}
          onBlur={(e) => {
            const v = e.target.value === "" ? null : Number(e.target.value);
            setAdj(row.original.player_id, "cpt_ownership", v, v !== null);
          }} />
      )}),
    ] : []),
    col.accessor("dvp_rank", { header: "DvP", cell: (c) => c.getValue() ?? "—" }),
    col.display({ id: "delta", header: "Δ", cell: ({ row }) => (
      <input type="number" step={0.5} className="w-14 px-1 py-0.5 text-right"
        defaultValue={(row.original.adjustments.delta as number) ?? ""}
        onBlur={(e) => {
          const v = e.target.value === "" ? null : Number(e.target.value);
          setAdj(row.original.player_id, "delta", v, v !== null && v !== 0);
        }} />
    )}),
    col.display({ id: "mult", header: "×", cell: ({ row }) => (
      <input type="number" step={0.05} className="w-14 px-1 py-0.5 text-right"
        defaultValue={(row.original.adjustments.multiplier as number) ?? ""}
        onBlur={(e) => {
          const v = e.target.value === "" ? null : Number(e.target.value);
          setAdj(row.original.player_id, "multiplier", v, v !== null && v !== 1);
        }} />
    )}),
  ], [slateId, profiles, showdown]);

  const all = pool.data?.players ?? [];
  // Teams ordered by game so opponents sit next to each other.
  const teamChips = useMemo(() => {
    const games = new Map<string, Set<string>>();
    for (const p of all) {
      if (!games.has(p.game_key)) games.set(p.game_key, new Set());
      games.get(p.game_key)!.add(p.team);
    }
    return [...games.entries()].sort(([a], [b]) => a.localeCompare(b))
      .map(([g, ts]) => ({ game: g, teams: [...ts].sort() }));
  }, [all]);

  const data = useMemo(() => all.filter((p) =>
    (pos === "ALL" || p.position === pos) &&
    (teams.size === 0 || teams.has(p.team)) &&
    (show === "all" || (show === "excluded") === !!p.adjustments.exclude) &&
    rules.every((r) => passes(p, r)) &&
    (!q || p.name.toLowerCase().includes(q.toLowerCase()) || p.team.toLowerCase() === q.toLowerCase()),
  ), [all, pos, teams, show, rules, q]);

  const activeRules = rules.filter((r) => r.value.trim() !== "");
  const filterNote = [pos !== "ALL" ? pos : "", teams.size ? [...teams].join("/") : "",
    ...activeRules.map((r) => `${fieldLabel(r.field)} ${r.op} ${r.value}`)].filter(Boolean).join(" · ");
  const toExclude = data.filter((p) => !p.adjustments.exclude && !p.adjustments.lock).map((p) => p.player_id);
  const toRestore = data.filter((p) => p.adjustments.exclude).map((p) => p.player_id);
  const nExcluded = all.filter((p) => p.adjustments.exclude).length;
  const filtered = pos !== "ALL" || teams.size > 0 || activeRules.length > 0 || show !== "all" || q !== "";

  const toggleTeam = (t: string) => setTeams((cur) => {
    const n = new Set(cur); if (n.has(t)) n.delete(t); else n.add(t); return n;
  });
  const setRule = (i: number, patch: Partial<Rule>) =>
    setRules((rs) => rs.map((r, k) => (k === i ? { ...r, ...patch } : r)));
  const clearFilters = () => { setPos("ALL"); setTeams(new Set()); setRules([]); setShow("all"); setQ(""); };
  const saveView = () => {
    const name = window.prompt("Name this filter", filterNote || "My filter")?.trim();
    if (!name) return;
    const next = [...views.filter((v) => v.name !== name), { name, pos, rules: activeRules }];
    setViews(next); storeViews(next);
  };
  const applyView = (name: string) => {
    const v = views.find((x) => x.name === name);
    if (!v) return;
    setPos(v.pos); setRules(v.rules); setTeams(new Set()); setShow("all");
  };
  const deleteView = (name: string) => {
    const next = views.filter((v) => v.name !== name);
    setViews(next); storeViews(next);
  };
  const [viewSel, setViewSel] = useState("");

  const table = useReactTable({
    data, columns, state: { sorting }, onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(), getSortedRowModel: getSortedRowModel(),
  });
  order.current = table.getRowModel().rows.map((r) => r.original.player_id);

  return (
    <div className="space-y-3">
      <div className="flex gap-2 items-center">
        <h1 className="eyebrow">Player pool</h1>
        <div className="ml-auto flex gap-2">
          {(showdown ? ["ALL", "QB", "RB", "WR", "TE", "K", "DST"] : ["ALL", "QB", "RB", "WR", "TE", "DST"]).map((p) => (
            <button key={p} onClick={() => setPos(p)}
              className={`px-2 py-1 rounded text-xs ${pos === p ? "bg-[var(--raised)]" : "text-[var(--dim)]"}`}>{p}</button>
          ))}
          <input placeholder="Search name / team" value={q} onChange={(e) => setQ(e.target.value)} className="w-44" />
        </div>
      </div>
      <div className="flex flex-wrap gap-x-3 gap-y-1 items-center text-xs">
        <span className="eyebrow">Teams</span>
        {teamChips.map(({ game, teams: ts }) => (
          <span key={game} className="flex gap-0.5">
            {ts.map((t) => (
              <button key={t} onClick={() => toggleTeam(t)}
                className={`px-1.5 py-0.5 rounded ${teams.has(t) ? "bg-[var(--raised)] text-[var(--ink)]" : "text-[var(--dim)]"}`}>{t}</button>
            ))}
          </span>
        ))}
      </div>
      <div className="flex flex-wrap gap-2 items-center text-xs">
        <span className="eyebrow">Filters</span>
        {rules.map((r, i) => (
          <span key={i} className="flex gap-1 items-center">
            <select value={r.field} onChange={(e) => setRule(i, { field: e.target.value as NumField })}>
              {FIELDS.filter((f) => showdown || !f.showdownOnly).map((f) => (
                <option key={f.key} value={f.key}>{f.label}</option>
              ))}
            </select>
            <select value={r.op} onChange={(e) => setRule(i, { op: e.target.value as Op })}>
              {["<", "<=", ">", ">="].map((o) => <option key={o} value={o}>{o}</option>)}
            </select>
            <input type="number" step="any" value={r.value} className="w-16 px-1 py-0.5 text-right"
              onChange={(e) => setRule(i, { value: e.target.value })} />
            <button className="text-[var(--dim)] hover:text-[var(--ink)]" title="Remove filter"
              onClick={() => setRules((rs) => rs.filter((_, k) => k !== i))}>✕</button>
          </span>
        ))}
        <Btn kind="ghost" onClick={() => setRules((rs) => [...rs, { field: "value", op: "<", value: "" }])}>+ Filter</Btn>
        <select value={show} onChange={(e) => setShow(e.target.value as typeof show)}>
          <option value="all">All players</option>
          <option value="active">Not excluded</option>
          <option value="excluded">Excluded only</option>
        </select>
        {filtered && <Btn kind="ghost" onClick={clearFilters}>Clear filters</Btn>}
        <span className="ml-auto flex gap-1 items-center">
          <select value={viewSel} onChange={(e) => { setViewSel(e.target.value); applyView(e.target.value); }}>
            <option value="">Saved filters…</option>
            {views.map((v) => <option key={v.name} value={v.name}>{v.name}</option>)}
          </select>
          {viewSel && <Btn kind="ghost" onClick={() => { deleteView(viewSel); setViewSel(""); }}>Delete</Btn>}
          <Btn kind="ghost" disabled={!activeRules.length && pos === "ALL"} onClick={saveView}>Save filter</Btn>
        </span>
      </div>
      {filtered && (
        <div className="flex gap-2 items-center text-xs">
          <Btn kind="danger" disabled={busy || !toExclude.length}
            onClick={() => setBulk(toExclude, "exclude", true, `bulk: ${filterNote || "filtered"}`)}>
            Exclude {toExclude.length} shown
          </Btn>
          <Btn kind="ghost" disabled={busy || !toRestore.length}
            onClick={() => setBulk(toRestore, "exclude", false)}>
            Un-exclude {toRestore.length} shown
          </Btn>
          <span className="text-[var(--dim)]">Locked players are skipped.</span>
        </div>
      )}
      {!pool.data?.has_sims && (
        <div className="text-xs text-[var(--amber)]">
          Floors and ceilings appear after the sims matrix is built (Overview → Simulate).
        </div>
      )}
      <div className="panel overflow-auto max-h-[75vh]">
        <table className="w-full">
          <thead className="sticky top-0 bg-[var(--panel)]">
            {table.getHeaderGroups().map((hg) => (
              <tr key={hg.id} className="border-b hairline">
                {hg.headers.map((h) => (
                  <th key={h.id} onClick={h.column.getToggleSortingHandler()}
                    className="px-2 py-1.5 text-left text-[10px] uppercase tracking-wider text-[var(--dim)] cursor-pointer select-none whitespace-nowrap">
                    {flexRender(h.column.columnDef.header, h.getContext())}
                    {{ asc: " ↑", desc: " ↓" }[h.column.getIsSorted() as string] ?? ""}
                  </th>
                ))}
              </tr>
            ))}
          </thead>
          <tbody>
            {table.getRowModel().rows.map((r) => (
              <tr key={r.id} className="border-b hairline hover:bg-[var(--raised)]">
                {r.getVisibleCells().map((c) => (
                  <td key={c.id} className="px-2 py-1 whitespace-nowrap">
                    {flexRender(c.column.columnDef.cell, c.getContext())}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <PlayerDrawer playerId={detail} slateId={slateId} onClose={() => setDetail(null)} />
      <div className="text-[11px] text-[var(--dim)]">
        {data.length} of {all.length} players · {nExcluded} excluded · Shift+click a 🔒 or ✕ box to set the range since the last one you clicked · Δ adds points, × multiplies the projection. Both persist for this slate.
        {showdown && " Captain projections are 1.5x these; lock and exclude cover both slots."}
      </div>
    </div>
  );
}
