import { useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import { Badge, Btn } from "../ui";

interface ResultContest {
  id: number; contest_key: string; name: string; draft_group_id: number | null;
  slate_type: string | null; season: number | null; week: number | null;
  max_entries_per_user: number | null; total_entries: number | null;
  large_field: boolean; mme: boolean; lf_mme: boolean;
  capture: { rows?: number; top_n?: number; full_field?: boolean; players?: number;
    unmatched?: number; max_entries_source?: string; salary_scoped_match?: boolean };
}
interface ListOut { rule: { lf_min_entries: number; mme_min_entries_per_user: number }; contests: ResultContest[] }
interface ImportOut {
  imported: { contest_key: string; name: string; unmatched: string[]; lf_mme: boolean }[];
  errors: { file?: string; contest_key?: string; error: string }[];
  dk_details: number;
}

/** Contest standings loaded for ownership history. Upload every standings
 *  CSV at once, with pull_summary.csv for leaderboard pulls. */
export default function ContestResults() {
  const qc = useQueryClient();
  const list = useQuery({ queryKey: ["contest-results"],
    queryFn: () => api.get<ListOut>("/api/contest-results") });
  const input = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [out, setOut] = useState<ImportOut | null>(null);
  const [error, setError] = useState("");

  const upload = async (files: FileList) => {
    setBusy(true); setError(""); setOut(null);
    try {
      const fd = new FormData();
      [...files].forEach((f) => fd.append("files", f));
      setOut(await api.post<ImportOut>("/api/contest-results/import", fd));
      qc.invalidateQueries({ queryKey: ["contest-results"] });
      qc.invalidateQueries({ queryKey: ["player"] });
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
      if (input.current) input.current.value = "";
    }
  };

  const rule = list.data?.rule;
  const rows = list.data?.contests ?? [];
  const unmatched = (out?.imported ?? []).flatMap((r) => r.unmatched.map((n) => `${n} (${r.contest_key})`));

  return (
    <section className="panel p-4 space-y-3">
      <div className="flex items-center gap-3">
        <h2 className="eyebrow">Contest results</h2>
        <span className="text-[10px] text-[var(--mute)]">{rows.length} contests · {rows.filter((r) => r.lf_mme).length} large-field MME</span>
        <div className="ml-auto">
          <input ref={input} type="file" accept=".csv" multiple hidden
            onChange={(e) => e.target.files?.length && upload(e.target.files)} />
          <Btn kind="primary" disabled={busy} onClick={() => input.current?.click()}>
            {busy ? "Importing…" : "Import standings CSVs"}
          </Btn>
        </div>
      </div>
      <div className="text-[11px] text-[var(--dim)] max-w-2xl">
        Select every contest-standings CSV and, for leaderboard pulls, pull_summary.csv, in one go. The file name
        supplies the contest key. Re-importing a contest replaces it.
        {rule && <> Large-field MME means at least {rule.lf_min_entries.toLocaleString()} entries and at least {rule.mme_min_entries_per_user} entries
        allowed per user. Only those Classic contests feed field own% on player pages.</>}
      </div>
      {error && <pre className="text-[11px] whitespace-pre-wrap border-l-2 border-[var(--line)] pl-2">{error}</pre>}
      {out && (
        <div className="text-[11px] space-y-1">
          <div className="text-[var(--ink)]">
            Imported {out.imported.length} contests · {out.dk_details} with DraftKings contest details.
          </div>
          {out.errors.map((e, i) => <div key={i} className="text-[var(--down)]">{e.file ?? e.contest_key}: {e.error}</div>)}
          {unmatched.length > 0 && (
            <div className="text-[var(--dim)]">Unmatched players ({unmatched.length}): {unmatched.slice(0, 30).join(", ")}{unmatched.length > 30 ? "…" : ""}</div>
          )}
        </div>
      )}
      {rows.length > 0 && (
        <table className="w-full text-[11px] num">
          <thead>
            <tr className="text-[9px] uppercase tracking-wider text-[var(--dim)]">
              {["Wk", "Contest", "Draft group", "Entries", "Max/user", "Rows", "Players", "Unmatched", ""].map((h) =>
                <th key={h} className="text-left font-normal pr-4 pb-1">{h}</th>)}
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id} className="border-t hairline">
                <td className="pr-4 py-1">{r.season && r.week ? `${r.season} wk${r.week}` : "—"}</td>
                <td className="pr-4 py-1 max-w-[320px] truncate" title={r.name}>{r.name}</td>
                <td className="pr-4 py-1 text-[var(--dim)]">{r.draft_group_id}</td>
                <td className="pr-4 py-1">{r.total_entries?.toLocaleString() ?? "—"}</td>
                <td className="pr-4 py-1" title={r.capture.max_entries_source ? `from ${r.capture.max_entries_source}` : undefined}>
                  {r.max_entries_per_user ?? "—"}{r.capture.max_entries_source === "observed" ? "*" : ""}
                </td>
                <td className="pr-4 py-1">{r.capture.rows}{r.capture.full_field ? " (full)" : ""}</td>
                <td className="pr-4 py-1">{r.capture.players}</td>
                <td className="pr-4 py-1">{r.capture.unmatched || ""}</td>
                <td className="py-1 space-x-1">
                  {r.lf_mme && <Badge tone="up">LF MME</Badge>}
                  {r.slate_type === "showdown" && <Badge>Showdown</Badge>}
                  {!r.season && <Badge tone="amber">no salaries</Badge>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {rows.some((r) => r.capture.max_entries_source === "observed") && (
        <div className="text-[10px] text-[var(--mute)]">
          * Max/user read from entry names in the file (a lower bound; no suffix in 200+ rows reads as single entry).
          DraftKings contest details replace it when the server can reach them.
        </div>
      )}
      {rows.some((r) => !r.season) && (
        <div className="text-[10px] text-[var(--mute)]">
          No salaries: the draft group has no salary history, so its ownership has no week on player pages.
          Fill that week under Salary history with the draft group ID.
        </div>
      )}
    </section>
  );
}
