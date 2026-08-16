import { useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { api } from "../api";
import { Badge, Btn, money, num } from "../ui";

interface ContestRow {
  id: number; contest_key: string; name: string; entry_fee: number | null;
  field_size: number | null; max_entries_per_user: number | null;
  n_entries: number; has_payout_curve: boolean;
}
interface Results {
  contest: { id: number; name: string; entry_fee: number | null };
  entries: { dk_entry_id: string; rank: number | null; points: number | null; payout: number | null }[];
  ownership: { player: string; position: string; drafted_pct: number | null; fpts: number | null; matched: boolean }[];
  roi: { fees: number; winnings: number; roi: number | null };
}

export default function Contests() {
  const { slateId } = useParams();
  const qc = useQueryClient();
  const contests = useQuery({ queryKey: ["contests", slateId],
    queryFn: () => api.get<ContestRow[]>(`/api/slates/${slateId}/contests`) });
  const sets = useQuery({ queryKey: ["sets", slateId],
    queryFn: () => api.get<any[]>(`/api/slates/${slateId}/sets`) });
  const entriesFile = useRef<HTMLInputElement>(null);
  const standingsFile = useRef<HTMLInputElement>(null);
  const [standingsFor, setStandingsFor] = useState<string | null>(null);
  const [exportSet, setExportSet] = useState<number | null>(null);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [results, setResults] = useState<Results | null>(null);
  const [msg, setMsg] = useState("");

  const uploadEntries = async (f: File) => {
    const fd = new FormData();
    fd.append("file", f);
    const r = await api.post<{ entries: number; new: number }>(`/api/slates/${slateId}/contests/import-entries`, fd);
    setMsg(`Imported ${r.entries} entries (${r.new} new)`);
    qc.invalidateQueries({ queryKey: ["contests", slateId] });
  };

  const uploadStandings = async (f: File) => {
    if (!standingsFor) return;
    const fd = new FormData();
    fd.append("file", f);
    const r = await api.post<any>(`/api/slates/${slateId}/contests/${standingsFor}/import-standings`, fd);
    setMsg(`Standings: ${r.entries} entries, ${r.ownership_rows} ownership rows`);
    qc.invalidateQueries({ queryKey: ["contests", slateId] });
  };

  const doExport = async () => {
    const csv = await api.post<string>(`/api/slates/${slateId}/contests/export`,
      { lineup_set_id: exportSet, contest_ids: [...selected] });
    const blob = new Blob([csv], { type: "text/csv" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "DKEntries_upload.csv";
    a.click();
  };

  const viewResults = async (id: number) => {
    setResults(await api.get<Results>(`/api/slates/${slateId}/contests/${id}/results`));
  };

  return (
    <div className="space-y-5">
      <div className="flex items-center gap-3">
        <h1 className="eyebrow">Contests & entries</h1>
        <div className="ml-auto flex gap-2 items-center">
          <input ref={entriesFile} type="file" accept=".csv" hidden
            onChange={(e) => e.target.files?.[0] && uploadEntries(e.target.files[0])} />
          <Btn kind="primary" onClick={() => entriesFile.current?.click()}>Import DKEntries CSV</Btn>
        </div>
      </div>
      {msg && <div className="text-xs text-[var(--up)]">{msg}</div>}

      <div className="panel divide-y divide-[var(--line)]">
        {contests.data?.map((c) => (
          <div key={c.id} className="flex items-center gap-3 px-4 py-2.5">
            <input type="checkbox" checked={selected.has(c.id)}
              onChange={(e) => {
                const n = new Set(selected);
                e.target.checked ? n.add(c.id) : n.delete(c.id);
                setSelected(n);
              }} />
            <span className="font-medium">{c.name}</span>
            <span className="num text-[var(--dim)]">{c.contest_key}</span>
            <span className="num">{money(c.entry_fee)}</span>
            <span className="num text-[var(--dim)]">{c.n_entries} entries</span>
            {c.has_payout_curve ? <Badge tone="up">payout curve</Badge> : <Badge>no curve</Badge>}
            <span className="ml-auto flex gap-2">
              <Btn kind="ghost" onClick={() => viewResults(c.id)}>Results</Btn>
              <Btn kind="ghost" onClick={() => { setStandingsFor(c.contest_key); standingsFile.current?.click(); }}>
                Import standings
              </Btn>
            </span>
          </div>
        ))}
        {contests.data?.length === 0 && (
          <div className="px-4 py-6 text-[var(--dim)]">
            Import the DKEntries CSV downloaded from DraftKings to create contests and entry reservations.
          </div>
        )}
      </div>
      <input ref={standingsFile} type="file" accept=".csv" hidden
        onChange={(e) => e.target.files?.[0] && uploadStandings(e.target.files[0])} />

      <div className="panel p-4 flex items-end gap-3">
        <div className="flex flex-col gap-1">
          <span className="eyebrow">Assign & export</span>
          <select value={exportSet ?? ""} onChange={(e) => setExportSet(Number(e.target.value))}>
            <option value="">Choose lineup set…</option>
            {sets.data?.map((s) => <option key={s.id} value={s.id}>#{s.id} {s.label} ({s.n_lineups})</option>)}
          </select>
        </div>
        <Btn kind="primary" disabled={!exportSet || selected.size === 0} onClick={doExport}>
          Export DKEntries upload for {selected.size} contest{selected.size === 1 ? "" : "s"}
        </Btn>
        <span className="text-[11px] text-[var(--dim)]">
          Lineups cycle across the selected contests' entries in order. Verify the first real upload — see export notes.
        </span>
      </div>

      {results && (
        <div className="grid grid-cols-2 gap-4">
          <div className="panel p-3">
            <div className="eyebrow mb-2">{results.contest.name} — entries</div>
            <div className="num text-xs mb-2">
              Fees {money(results.roi.fees)} · Winnings {money(results.roi.winnings)} ·
              ROI <span className={(results.roi.roi ?? 0) >= 0 ? "text-[var(--ink)]" : "text-[var(--dim)]"}>
                {results.roi.roi === null ? "—"
                  : `${results.roi.roi >= 0 ? "▲" : "▼"} ${Math.abs(results.roi.roi * 100).toFixed(1)}%`}
              </span>
            </div>
            <div className="max-h-64 overflow-auto">
              {results.entries.map((e) => (
                <div key={e.dk_entry_id} className="flex gap-3 text-[11px] num py-0.5 border-b hairline">
                  <span className="w-12 text-[var(--dim)]">#{e.rank ?? "—"}</span>
                  <span className="flex-1">{e.dk_entry_id}</span>
                  <span>{num(e.points)}</span>
                  <span className="w-14 text-right">{e.payout !== null ? money(e.payout) : "—"}</span>
                </div>
              ))}
            </div>
          </div>
          <div className="panel p-3">
            <div className="eyebrow mb-2">Realised ownership (training signal)</div>
            <div className="max-h-72 overflow-auto">
              {results.ownership.map((o, i) => (
                <div key={i} className="flex gap-3 text-[11px] num py-0.5 border-b hairline">
                  <span className="flex-1">{o.player} <span className="text-[var(--dim)]">{o.position}</span></span>
                  <span>{num(o.drafted_pct)}%</span>
                  <span className="w-12 text-right">{num(o.fpts)}</span>
                  {!o.matched && <span className="text-[var(--amber)]" title="Unresolved name">?</span>}
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
