import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import {
  createColumnHelper, flexRender, getCoreRowModel,
  getSortedRowModel, SortingState, useReactTable,
} from "@tanstack/react-table";
import { api, PoolPlayer } from "../api";
import { money, num } from "../ui";

const col = createColumnHelper<PoolPlayer>();

export default function Pool() {
  const { slateId } = useParams();
  const qc = useQueryClient();
  const pool = useQuery({ queryKey: ["pool", slateId],
    queryFn: () => api.get<{ players: PoolPlayer[]; has_sims: boolean }>(`/api/slates/${slateId}/pool`) });
  const [pos, setPos] = useState("ALL");
  const [q, setQ] = useState("");
  const [sorting, setSorting] = useState<SortingState>([{ id: "salary", desc: true }]);

  const setAdj = async (playerId: number, kind: string, value: number | null, on: boolean) => {
    if (on) await api.post(`/api/slates/${slateId}/pool/adjustments`, { player_id: playerId, kind, value });
    else await api.del(`/api/slates/${slateId}/pool/adjustments/${playerId}/${kind}`);
    qc.invalidateQueries({ queryKey: ["pool", slateId] });
  };

  const columns = useMemo(() => [
    col.display({ id: "lock", header: "🔒", cell: ({ row }) => {
      const a = row.original.adjustments;
      return <input type="checkbox" checked={!!a.lock} title="Lock"
        onChange={(e) => setAdj(row.original.player_id, "lock", null, e.target.checked)} />;
    }}),
    col.display({ id: "excl", header: "✕", cell: ({ row }) => {
      const a = row.original.adjustments;
      return <input type="checkbox" checked={!!a.exclude} title="Exclude"
        onChange={(e) => setAdj(row.original.player_id, "exclude", null, e.target.checked)} />;
    }}),
    col.accessor("name", { header: "Player", cell: (c) => (
      <span className={c.row.original.adjustments.exclude ? "line-through text-[var(--dim)]" : ""}>
        {c.getValue()}
        {c.row.original.status && <span className="ml-1 text-[var(--down)] text-[10px]">{c.row.original.status}</span>}
      </span>
    )}),
    col.accessor("position", { header: "Pos" }),
    col.accessor("team", { header: "Tm" }),
    col.accessor("opponent", { header: "Opp" }),
    col.accessor("salary", { header: "Salary", cell: (c) => money(c.getValue()) }),
    col.accessor("projection", { header: "Proj", cell: (c) => num(c.getValue()) }),
    col.accessor("floor", { header: "Floor", cell: (c) => <span className="text-[var(--down)]">{num(c.getValue())}</span> }),
    col.accessor("ceiling", { header: "Ceil", cell: (c) => <span className="text-[var(--up)]">{num(c.getValue())}</span> }),
    col.accessor("value", { header: "Val", cell: (c) => num(c.getValue(), 2) }),
    col.accessor("ownership", { header: "Own%", cell: (c) => num(c.getValue()) }),
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
  ], [slateId]);

  const data = useMemo(() => (pool.data?.players ?? []).filter((p) =>
    (pos === "ALL" || p.position === pos) &&
    (!q || p.name.toLowerCase().includes(q.toLowerCase()) || p.team.toLowerCase() === q.toLowerCase()),
  ), [pool.data, pos, q]);

  const table = useReactTable({
    data, columns, state: { sorting }, onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(), getSortedRowModel: getSortedRowModel(),
  });

  return (
    <div className="space-y-3">
      <div className="flex gap-2 items-center">
        <h1 className="eyebrow">Player pool</h1>
        <div className="ml-auto flex gap-2">
          {["ALL", "QB", "RB", "WR", "TE", "DST"].map((p) => (
            <button key={p} onClick={() => setPos(p)}
              className={`px-2 py-1 rounded text-xs ${pos === p ? "bg-[var(--raised)]" : "text-[var(--dim)]"}`}>{p}</button>
          ))}
          <input placeholder="Search name / team" value={q} onChange={(e) => setQ(e.target.value)} className="w-44" />
        </div>
      </div>
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
      <div className="text-[11px] text-[var(--dim)]">{data.length} players · Δ adds points, × multiplies the projection. Both persist for this slate.</div>
    </div>
  );
}
