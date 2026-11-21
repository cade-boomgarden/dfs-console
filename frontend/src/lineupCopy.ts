/** Lineup → clipboard text, for pasting into notes (Obsidian etc.).
 *
 *  Pure formatting: callers pass rows already adjusted for the slot they
 *  play in (Showdown CPT rows carry 1.5x salary/points and CPT ownership).
 *  Empty slots are kept as "—" rows so a partial build reads as partial. */

export interface CopyRow {
  slot: string;
  name: string | null;      // null = empty slot
  salary: number;
  projection: number;
  ownership: number;        // percent, e.g. 12.3
}

export type CopyFormat = "markdown" | "text";

const fmtMoney = (v: number) => `$${Math.round(v).toLocaleString("en-US")}`;
const fmtNum = (v: number) => v.toFixed(1);
const fmtPct = (v: number) => `${v.toFixed(1)}%`;

const HEADER = ["Slot", "Player", "Salary", "Proj", "Own%"];

function cells(rows: CopyRow[]): { body: string[][]; total: string[] } {
  const filled = rows.filter((r) => r.name !== null);
  const sum = (k: "salary" | "projection" | "ownership") => filled.reduce((a, r) => a + r[k], 0);
  const body = rows.map((r) => r.name === null
    ? [r.slot, "—", "—", "—", "—"]
    : [r.slot, r.name, fmtMoney(r.salary), fmtNum(r.projection), fmtPct(r.ownership)]);
  const total = ["Total", `${filled.length}/${rows.length} players`,
    fmtMoney(sum("salary")), fmtNum(sum("projection")), fmtPct(sum("ownership"))];
  return { body, total };
}

export function lineupMarkdown(rows: CopyRow[]): string {
  const { body, total } = cells(rows);
  const esc = (s: string) => s.replace(/\|/g, "\\|");
  const line = (c: string[]) => `| ${c.map(esc).join(" | ")} |`;
  return [
    line(HEADER),
    "| --- | --- | ---: | ---: | ---: |",
    ...body.map(line),
    line(total.map((c) => `**${c}**`)),
  ].join("\n");
}

export function lineupText(rows: CopyRow[]): string {
  const { body, total } = cells(rows);
  const all = [HEADER, ...body, total];
  const width = HEADER.map((_, i) => Math.max(...all.map((r) => r[i].length)));
  // text columns left-aligned, numbers right-aligned
  const pad = (c: string, i: number) => (i < 2 ? c.padEnd(width[i]) : c.padStart(width[i]));
  const line = (r: string[]) => r.map(pad).join("  ").trimEnd();
  const rule = width.map((w) => "-".repeat(w)).join("  ");
  return [line(HEADER), rule, ...body.map(line), rule, line(total)].join("\n");
}

export function formatLineup(rows: CopyRow[], format: CopyFormat): string {
  return format === "markdown" ? lineupMarkdown(rows) : lineupText(rows);
}

/** Clipboard write with a fallback for non-secure contexts (plain-http LAN dev). */
export async function copyText(text: string): Promise<void> {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  try {
    if (!document.execCommand("copy")) throw new Error("Copy failed");
  } finally {
    document.body.removeChild(ta);
  }
}
