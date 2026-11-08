import { useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, PlayerDetailDTO, PlayerGame, PlayerWeek } from "../api";
import { money, num } from "../ui";
import { fmt, Fmt, PctBar, ROWS } from "./PlayerCard";
import { WeekChart, WeekSeries } from "./WeekChart";

type Col = [key: string, label: string, get: (g: PlayerGame) => string];
const b = (k: string) => (g: PlayerGame) => String(g.box[k] ?? 0);
const u = (k: string, f: Fmt) => (g: PlayerGame) => (g.usage[k] === undefined ? "—" : fmt(g.usage[k], f));

const PASS: Col[] = [
  ["cmp", "Cmp/Att", (g) => `${g.box.completions ?? 0}/${g.box.attempts ?? 0}`],
  ["pyd", "Pass yd", b("passing_yards")], ["ptd", "Pass TD", b("passing_tds")],
  ["int", "INT", b("passing_interceptions")],
];
const RUSH: Col[] = [
  ["car", "Car", b("carries")], ["ryd", "Rush yd", b("rushing_yards")], ["rtd", "Rush TD", b("rushing_tds")],
];
const RECV: Col[] = [
  ["tgt", "Tgt", b("targets")], ["rec", "Rec", b("receptions")],
  ["cyd", "Rec yd", b("receiving_yards")], ["ctd", "Rec TD", b("receiving_tds")],
];
const LOG_COLS: Record<string, Col[]> = {
  QB: [...PASS, ...RUSH, ["snap", "Snap%", u("snap_share", "pct")], ["adot", "aDOT", u("adot", "yd")]],
  RB: [...RUSH, ...RECV, ["snap", "Snap%", u("snap_share", "pct")],
       ["cs", "Carry sh", u("carry_share", "pct")], ["ts", "Tgt sh", u("target_share", "pct")]],
  WR: [...RECV, ["snap", "Snap%", u("snap_share", "pct")], ["ts", "Tgt sh", u("target_share", "pct")],
       ["ays", "AY sh", u("air_yards_share", "pct")], ["adot", "aDOT", u("rec_adot", "yd")]],
  TE: [...RECV, ["snap", "Snap%", u("snap_share", "pct")], ["ts", "Tgt sh", u("target_share", "pct")],
       ["ays", "AY sh", u("air_yards_share", "pct")], ["adot", "aDOT", u("rec_adot", "yd")]],
};

const mean = (xs: number[]) => (xs.length ? xs.reduce((a, c) => a + c, 0) / xs.length : null);
const sk = (v: number) => `$${(v / 1000).toFixed(1)}k`;

function Tile({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="panel px-3 py-2 min-w-[110px]">
      <div className="eyebrow">{label}</div>
      <div className="num text-[15px] text-[var(--ink)]">{value}</div>
      {sub && <div className="num text-[10px] text-[var(--dim)]">{sub}</div>}
    </div>
  );
}

/** Everything about one player: this slate, salary and points by week,
 *  per-game usage with season-to-date percentiles, and the game log. */
export function PlayerDetail({ playerId, slateId }: { playerId: number; slateId?: string }) {
  const q = useQuery({
    queryKey: ["player", playerId, slateId],
    queryFn: () => api.get<PlayerDetailDTO>(`/api/players/${playerId}${slateId ? `?slate_id=${slateId}` : ""}`),
  });
  const d = q.data;
  const seasons = useMemo(() => {
    const s = new Set<number>();
    d?.weeks.forEach((w) => w.season && s.add(w.season));
    d?.games.forEach((g) => s.add(g.season));
    return [...s].sort((a, b) => b - a);
  }, [d]);
  const [season, setSeason] = useState<number | null>(null);
  useEffect(() => { if (seasons.length && (season === null || !seasons.includes(season))) setSeason(seasons[0]); },
    [seasons, season]);

  if (q.isLoading) return <div className="p-4 text-[var(--dim)]">Loading…</div>;
  if (q.isError || !d) return <div className="p-4 text-[var(--dim)]">{(q.error as Error)?.message ?? "Not found"}</div>;

  const pos = d.player.position;
  const sw: PlayerWeek[] = d.weeks.filter((w) => w.season === season && w.week);
  const sg: PlayerGame[] = d.games.filter((g) => g.season === season);
  const allWeeks = [...sw.map((w) => w.week!), ...sg.map((g) => g.week)];
  const lastWeek = allWeeks.length ? Math.max(...allWeeks) : 0;
  const weeks = Array.from({ length: lastWeek }, (_, i) => i + 1);
  // one salary row per week: prefer the ingested (main) slate
  const byWeek = new Map<number, PlayerWeek>();
  sw.forEach((w) => { const cur = byWeek.get(w.week!); if (!cur || (cur.source !== "ingest" && w.source === "ingest")) byWeek.set(w.week!, w); });
  const gByWeek = new Map(sg.map((g) => [g.week, g]));

  const salary: WeekSeries = { key: "sal", label: "Salary", kind: "line",
    values: new Map(weeks.map((w) => [w, byWeek.get(w)?.salary])) };
  const pts: WeekSeries = { key: "pts", label: "DK points", kind: "bar",
    values: new Map(weeks.map((w) => [w, gByWeek.get(w)?.dk_points ?? byWeek.get(w)?.dk_points])) };
  const proj: WeekSeries = { key: "proj", label: "Projection", kind: "tick",
    values: new Map(weeks.map((w) => [w, byWeek.get(w)?.projection])) };

  const pointsList = sg.map((g) => g.dk_points);
  const last3 = mean([...sg].sort((a, b) => b.week - a.week).slice(0, 3).map((g) => g.dk_points));
  const salList = weeks.map((w) => byWeek.get(w)?.salary).filter((v): v is number => !!v);
  const salDelta = salList.length > 1 ? salList[salList.length - 1] - salList[0] : null;
  const ptsPerK = (() => {
    const pairs = weeks.map((w) => [byWeek.get(w)?.salary, gByWeek.get(w)?.dk_points] as const)
      .filter((p): p is readonly [number, number] => !!p[0] && p[1] !== undefined);
    return pairs.length ? (pairs.reduce((a, [, p]) => a + p, 0) / pairs.reduce((a, [s]) => a + s, 0)) * 1000 : null;
  })();
  const beat = weeks.map((w) => [byWeek.get(w)?.projection, gByWeek.get(w)?.dk_points] as const)
    .filter((p): p is readonly [number, number] => p[0] != null && p[1] !== undefined && p[0] > 0);

  const prof = d.profile && d.profile.season === season ? d.profile : null;
  const rows = (ROWS[pos] ?? []);
  const trendAt = (k: string, gameWeek: number) =>
    (d.pct_trend[k] ?? []).find((t) => t.week === gameWeek + 1);   // as-of wk N = games thru N-1
  const cols = LOG_COLS[pos] ?? [];
  const missing = weeks.filter((w) => !byWeek.has(w) && gByWeek.has(w));

  return (
    <div className="space-y-5">
      <div className="flex items-end gap-3 flex-wrap">
        <div>
          <div className="text-lg font-semibold leading-tight">{d.player.name}</div>
          <div className="text-[var(--dim)] text-xs num">
            {pos} · {d.current?.team ?? d.player.team}
            {d.current && <> v {d.current.opponent}</>}
            {d.current?.status && <span className="ml-1">{d.current.status}</span>}
          </div>
        </div>
        {d.profile?.label && (
          <span className="px-1.5 py-0.5 rounded text-[10px] uppercase tracking-wider border hairline text-[var(--dim)]">
            {d.profile.label}
          </span>
        )}
        <div className="ml-auto flex gap-1">
          {seasons.map((s) => (
            <button key={s} onClick={() => setSeason(s)} aria-pressed={season === s}
              className={`px-2 py-0.5 rounded text-[11px] num ${season === s ? "bg-[var(--raised)] text-[var(--ink)]" : "text-[var(--dim)] hover:text-[var(--ink)]"}`}>
              {s}
            </button>
          ))}
        </div>
      </div>

      <div className="flex gap-2 flex-wrap">
        {d.current && <>
          <Tile label="Salary" value={money(d.current.salary)}
            sub={salDelta !== null ? `${salDelta >= 0 ? "+" : "−"}${money(Math.abs(salDelta))} since wk${weeks.find((w) => byWeek.has(w))}` : undefined} />
          <Tile label="Projection" value={num(d.current.projection)} sub={`${num(d.current.value, 2)} pts/$1k`} />
          <Tile label="Floor · Ceil" value={`${num(d.current.floor)} · ${num(d.current.ceiling)}`} />
          <Tile label="Own%" value={num(d.current.ownership)}
            sub={d.current.implied_opp_total ? `opp implied ${num(d.current.implied_opp_total)}` : undefined} />
        </>}
        <Tile label={`${season ?? ""} DK pts/g`} value={num(mean(pointsList))}
          sub={last3 !== null ? `last 3: ${num(last3)}` : undefined} />
        <Tile label="Pts per $1k" value={num(ptsPerK, 2)} sub="weeks with salary" />
        {beat.length > 0 && (
          <Tile label="Beat projection" value={`${beat.filter(([p, a]) => a > p).length}/${beat.length}`}
            sub={`avg ${num(mean(beat.map(([p, a]) => a - p)))} vs proj`} />
        )}
      </div>

      {weeks.length > 0 ? (
        <section className="panel p-3 space-y-4">
          <WeekChart title="Salary" weeks={weeks} series={[salary]} format={sk} yZero={false} height={84}
            extra={(w) => { const r = byWeek.get(w); return r ? (
              <div className="text-[var(--dim)]">{r.team} v {r.opponent}{r.source === "backfill" ? " · backfilled" : ""}</div>) : null; }} />
          <WeekChart title="DK points" legend weeks={weeks} series={[pts, proj]} format={(v) => v.toFixed(1)} height={110}
            extra={(w) => { const r = byWeek.get(w); return r?.actual_own != null ? (
              <div className="flex justify-between gap-4"><span className="text-[var(--dim)]">Field own%</span><span>{num(r.actual_own)}</span></div>) : null; }} />
          {missing.length > 0 && (
            <div className="text-[10px] text-[var(--mute)]">
              No salary for week {missing.join(", ")}. Fill it from Slates → Salary history.
            </div>
          )}
        </section>
      ) : (
        <div className="text-[11px] text-[var(--dim)]">No salary history or games for this player yet.</div>
      )}

      {pos !== "DST" && rows.length > 0 && sg.length > 0 && (
        <section>
          <div className="flex items-baseline gap-2 mb-2">
            <h2 className="eyebrow">Usage by game</h2>
            <span className="text-[10px] text-[var(--mute)]">
              {prof
                ? <>Value and percentile: {prof.season} season to date ({prof.games} {prof.games === 1 ? "game" : "games"}),
                    vs {pos}s with {prof.min_games}+ games. Hover a week for that game and the percentile through it.</>
                : <>Per game. Percentiles cover the current season only.</>}
            </span>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
            {rows.map(([k, label, f]) => {
              const feat = prof?.features?.[k];
              const series: WeekSeries = { key: k, label, kind: "line",
                values: new Map(weeks.map((w) => [w, gByWeek.get(w)?.usage[k]])) };
              if (![...series.values.values()].some((v) => v !== undefined)) return null;
              return (
                <div key={k} className="panel p-2">
                  <div className="flex items-center gap-2 text-[11px] mb-1">
                    <span className="text-[var(--dim)]">{label}</span>
                    <span className="ml-auto num">{feat ? fmt(feat.value, f) : "—"}</span>
                    {prof && <span className="num"><PctBar pct={feat?.pct ?? null} width={48} /></span>}
                  </div>
                  <WeekChart weeks={weeks} series={[series]} height={56}
                    format={(v) => fmt(v, f)}
                    yCap={f === "pct" ? 1 : undefined} yMinMax={f === "pct" ? 0.05 : 1}
                    extra={(w) => { const t = trendAt(k, w); return t ? (
                      <div className="flex justify-between gap-4">
                        <span className="text-[var(--dim)]">Season thru wk{w}</span>
                        <span>{fmt(t.value, f)}{t.pct !== null ? ` · pctl ${t.pct}` : ""}</span>
                      </div>) : null; }} />
                </div>
              );
            })}
          </div>
        </section>
      )}

      {(sg.length > 0 || sw.length > 0) && (
        <section>
          <h2 className="eyebrow mb-2">Game log · {season}</h2>
          <div className="panel overflow-auto">
            <table className="w-full text-[11px] num">
              <thead>
                <tr className="border-b hairline">
                  {["Wk", "Opp", "Salary", "Proj", "DK pts", ...cols.map((c) => c[1])].map((h) => (
                    <th key={h} className="px-2 py-1.5 text-left text-[9px] uppercase tracking-wider text-[var(--dim)] font-normal whitespace-nowrap">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {[...weeks].reverse().filter((w) => byWeek.has(w) || gByWeek.has(w)).map((w) => {
                  const r = byWeek.get(w), g = gByWeek.get(w);
                  const over = r?.projection != null && g ? g.dk_points - r.projection : null;
                  return (
                    <tr key={w} className="border-b hairline">
                      <td className="px-2 py-1">{w}</td>
                      <td className="px-2 py-1 text-[var(--dim)]">{g?.opponent ?? r?.opponent ?? "—"}</td>
                      <td className="px-2 py-1">{r ? money(r.salary) : "—"}</td>
                      <td className="px-2 py-1 text-[var(--dim)]">{num(r?.projection)}</td>
                      <td className="px-2 py-1">
                        {g ? num(g.dk_points) : "—"}
                        {over !== null && <span className="ml-1 text-[var(--mute)]">{over >= 0 ? "+" : "−"}{Math.abs(over).toFixed(1)}</span>}
                      </td>
                      {cols.map(([key, , get]) => (
                        <td key={key} className="px-2 py-1">{g ? get(g) : "—"}</td>
                      ))}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          {sg.length === 0 && pos !== "DST" && (
            <div className="text-[10px] text-[var(--mute)] mt-1">No game logs. On the Slates page, click Refresh player profiles.</div>
          )}
        </section>
      )}
    </div>
  );
}

/** Right-side drawer: the detail without leaving Pool or Builder, so a
 *  lineup in progress survives. Esc or the backdrop closes it. */
export function PlayerDrawer({ playerId, slateId, onClose }: {
  playerId: number | null; slateId?: string; onClose: () => void;
}) {
  useEffect(() => {
    const h = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [onClose]);
  if (playerId === null) return null;
  return createPortal(
    <div className="fixed inset-0 z-40 flex justify-end">
      <div className="absolute inset-0 bg-black/50" onClick={onClose} />
      <div className="relative h-full w-[min(920px,96vw)] bg-[var(--bg)] border-l hairline overflow-auto p-4">
        <div className="flex items-center gap-2 mb-3">
          <Link to={`/slate/${slateId}/player/${playerId}`} className="text-[11px] text-[var(--dim)] hover:text-[var(--ink)]">
            Open as page ↗
          </Link>
          <button onClick={onClose} className="ml-auto text-[var(--dim)] hover:text-[var(--ink)] px-2" title="Close (Esc)">✕</button>
        </div>
        <PlayerDetail playerId={playerId} slateId={slateId} />
      </div>
    </div>,
    document.body,
  );
}

/** The eye: opens the player drawer. Stops the click so a row's own
 *  handler (Builder picks the player on row click) does not fire. */
export function EyeButton({ onClick, title = "Player detail", asSpan }: {
  onClick: () => void; title?: string; asSpan?: boolean;
}) {
  const props = {
    title, "aria-label": title,
    onClick: (e: React.MouseEvent) => { e.stopPropagation(); onClick(); },
    className: "inline-flex shrink-0 items-center justify-center w-6 h-5 rounded text-[var(--mute)] hover:text-[var(--ink)] hover:bg-[var(--raised)] cursor-pointer",
  };
  const icon = (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z" />
      <circle cx="12" cy="12" r="3" />
    </svg>
  );
  // a <button> cannot nest inside another button (Builder's slot rows)
  return asSpan
    ? <span role="button" tabIndex={0} {...props}>{icon}</span>
    : <button type="button" {...props}>{icon}</button>;
}
