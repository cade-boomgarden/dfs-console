import { useEffect, useRef, useState } from "react";
import { CopyFormat, CopyRow, copyText, formatLineup } from "../lineupCopy";

const OPTIONS: { key: CopyFormat; label: string; hint: string }[] = [
  { key: "markdown", label: "Markdown table", hint: "Pastes as a table in Obsidian" },
  { key: "text", label: "Plain text", hint: "Aligned columns, works anywhere" },
];

/** Copy icon with a two-item menu: Markdown table or plain text. */
export function CopyLineup({ rows, disabled }: { rows: CopyRow[]; disabled?: boolean }) {
  const [open, setOpen] = useState(false);
  const [status, setStatus] = useState<"" | "Copied" | "Copy failed">("");
  const ref = useRef<HTMLDivElement>(null);
  const timer = useRef<number>();

  // close the menu on any click outside it, or on Escape
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => { if (!ref.current?.contains(e.target as Node)) setOpen(false); };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("mousedown", onDown); document.removeEventListener("keydown", onKey); };
  }, [open]);
  useEffect(() => () => window.clearTimeout(timer.current), []);

  const copy = async (format: CopyFormat) => {
    setOpen(false);
    try { await copyText(formatLineup(rows, format)); setStatus("Copied"); }
    catch { setStatus("Copy failed"); }
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setStatus(""), 1500);
  };

  return (
    <div ref={ref} className="relative flex items-center gap-1">
      {status && (
        <span className={`text-[10px] uppercase tracking-wider ${status === "Copied" ? "text-[var(--up)]" : "text-[var(--down)]"}`}>
          {status}
        </span>
      )}
      <button type="button" disabled={disabled} onClick={() => setOpen((o) => !o)}
        title="Copy lineup" aria-label="Copy lineup" aria-haspopup="menu" aria-expanded={open}
        className="p-1 rounded text-[var(--dim)] hover:text-[var(--ink)] disabled:opacity-40 disabled:cursor-not-allowed">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <rect x="9" y="9" width="13" height="13" rx="2" />
          <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
        </svg>
      </button>
      {open && (
        <div role="menu" className="absolute right-0 top-full mt-1 z-20 w-44 panel border hairline rounded shadow-lg py-1 bg-[var(--panel)]">
          {OPTIONS.map((o) => (
            <button key={o.key} role="menuitem" type="button" onClick={() => copy(o.key)} title={o.hint}
              className="w-full text-left px-3 py-1.5 text-xs hover:bg-[var(--raised)]">
              {o.label}
              <div className="text-[10px] text-[var(--dim)]">{o.hint}</div>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
