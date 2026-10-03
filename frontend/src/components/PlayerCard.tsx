import { ReactNode, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useQuery } from "@tanstack/react-query";
import { api, PlayerProfile, PoolPlayer } from "../api";

/** Usage profiles for every pool player, fetched once per slate and shared
 *  by Pool and Builder. Keyed by player_id; players without a snapshot
 *  (rookies, DST) are absent. */
export function usePoolProfiles(slateId: string | undefined) {
  const q = useQuery({
    queryKey: ["pool-profiles", slateId],
    queryFn: () => api.get<{ players: Record<string, PlayerProfile> }>(`/api/slates/${slateId}/pool/profiles`),
    staleTime: 10 * 60_000,
    enabled: !!slateId,
  });
  return q.data?.players ?? {};
}

type Fmt = "pct" | "yd" | "x2";
type Row = [key: string, label: string, fmt: Fmt];

const RECEIVING: Row[] = [
  ["snap_share", "Snap share", "pct"],
  ["target_share", "Target share", "pct"],
  ["air_yards_share", "Air yards share", "pct"],
  ["wopr", "WOPR", "x2"],
  ["rec_adot", "aDOT", "yd"],
  ["deep_target_rate", "Deep target rate (20+)", "pct"],
  ["ez_target_share", "End-zone target share", "pct"],
  ["targets_per_dropback", "Targets per team dropback", "pct"],
  ["ypr", "Yards per catch", "yd"],
  ["yac_per_rec", "YAC per catch", "yd"],
];

const ROWS: Record<string, Row[]> = {
  QB: [
    ["snap_share", "Snap share", "pct"],
    ["ypa", "Yards per attempt", "yd"],
    ["adot", "aDOT", "yd"],
    ["deep_rate", "Deep attempt rate (20+)", "pct"],
    ["sack_rate", "Sack rate", "pct"],
    ["int_rate", "INT rate", "pct"],
    ["rush_att_per_dropback", "Rushes per dropback", "pct"],
    ["designed_rush_rate", "Designed rush rate", "pct"],
    ["scramble_rate", "Scramble rate", "pct"],
    ["ypc", "Yards per carry", "yd"],
  ],
  RB: [
    ["snap_share", "Snap share", "pct"],
    ["carry_share", "RB carry share", "pct"],
    ["gl_carry_share", "Goal-line carry share (≤5)", "pct"],
    ["target_share", "Target share", "pct"],
    ["targets_per_dropback", "Targets per team dropback", "pct"],
    ["wopr", "WOPR", "x2"],
    ["ypc", "Yards per carry", "yd"],
    ["ypr", "Yards per catch", "yd"],
    ["yac_per_rec", "YAC per catch", "yd"],
  ],
  WR: RECEIVING,
  TE: RECEIVING,
};

function fmt(v: number, f: Fmt): string {
  if (f === "pct") return `${(v * 100).toFixed(1)}%`;
  if (f === "x2") return v.toFixed(2);
  return v.toFixed(1);
}

function Card({ player, profile }: { player: PoolPlayer; profile?: PlayerProfile }) {
  const feats = profile?.features ?? null;
  const rows = (ROWS[player.position] ?? []).filter(([k]) => feats?.[k]);
  return (
    <div className="panel shadow-2xl shadow-black/60 w-[340px] p-3 space-y-2 text-xs">
      <div>
        <div className="flex items-center gap-2">
          <span className="font-bold text-[13px] leading-tight truncate">{player.name}</span>
          {profile?.label && (
            <span className="ml-auto shrink-0 px-1.5 py-0.5 rounded text-[10px] uppercase tracking-wider border hairline text-[var(--dim)] whitespace-nowrap">
              {profile.label}
            </span>
          )}
        </div>
        <div className="text-[var(--dim)] num text-[11px]">
          {player.position} · {player.team} v {player.opponent}
        </div>
        {profile?.features && profile.week > 1 && (
          <div className="text-[var(--dim)] num text-[11px]">
            {profile.season} season · {profile.games} {profile.games === 1 ? "game" : "games"} thru wk{profile.week - 1}
          </div>
        )}
      </div>

      {!profile ? (
        <div className="text-[var(--dim)] text-[11px]">
          No usage profile. Rookie or no recent NFL snaps; the sim uses a projection-based cold start.
        </div>
      ) : !feats ? (
        <div className="text-[var(--dim)] text-[11px]">
          No season stats yet. On the Slates page, click Refresh player profiles.
        </div>
      ) : rows.length === 0 ? (
        <div className="text-[var(--dim)] text-[11px]">
          No {profile.season} games before wk{profile.week}.
        </div>
      ) : (
        <>
          <table className="w-full">
            <thead>
              <tr className="text-[9px] uppercase tracking-wider text-[var(--dim)]">
                <th className="text-left font-normal pb-1"></th>
                <th className="text-right font-normal pb-1">Value</th>
                <th className="text-left font-normal pb-1 pl-3">Pctl vs {player.position}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(([k, label, f]) => {
                const feat = feats[k];
                return (
                  <tr key={k} title={feat.n !== null ? `Sample: ${Math.round(feat.n).toLocaleString()}` : undefined}>
                    <td className="py-0.5 pr-2 text-[var(--dim)] font-[family-name:var(--font-ui)] text-[11px]">{label}</td>
                    <td className="py-0.5 text-right">{fmt(feat.value, f)}</td>
                    <td className="py-0.5 pl-3">
                      {feat.pct === null ? <span className="text-[var(--mute)]">—</span> : (
                        <span className="flex items-center gap-1.5">
                          <span className="relative h-1.5 w-16 rounded bg-[var(--raised)] overflow-hidden">
                            <span className="absolute inset-y-0 left-0 bg-[var(--dim)]" style={{ width: `${feat.pct}%` }} />
                          </span>
                          <span className="w-6 text-right">{feat.pct}</span>
                        </span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <div className="text-[10px] text-[var(--mute)] leading-snug">
            {profile.season} season to date, unweighted. Percentile vs {player.position}s
            with {profile.min_games}+ {profile.min_games === 1 ? "game" : "games"} this season.
          </div>
        </>
      )}
    </div>
  );
}

/** Wrap a player's name. Hovering for 150 ms shows the usage card beside it.
 *  The card renders in a portal so scrolling tables cannot clip it, and it
 *  ignores the pointer so it never blocks the row underneath. */
export function PlayerHover({ player, profile, children, className }: {
  player: PoolPlayer; profile?: PlayerProfile; children: ReactNode; className?: string;
}) {
  const [anchor, setAnchor] = useState<DOMRect | null>(null);
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null);
  const timer = useRef<number>();
  const card = useRef<HTMLDivElement>(null);

  useLayoutEffect(() => {
    if (!anchor || !card.current) { setPos(null); return; }
    const { width, height } = card.current.getBoundingClientRect();
    const gap = 8, margin = 8;
    let left = anchor.right + gap;
    if (left + width > window.innerWidth - margin) left = Math.max(margin, anchor.left - gap - width);
    const top = Math.min(Math.max(margin, anchor.top - 8), window.innerHeight - height - margin);
    setPos({ left, top });
  }, [anchor]);

  const enter = (e: React.MouseEvent<HTMLElement>) => {
    const el = e.currentTarget;
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setAnchor(el.getBoundingClientRect()), 150);
  };
  const leave = () => { window.clearTimeout(timer.current); setAnchor(null); };

  if (player.position === "DST") return <span className={className}>{children}</span>;

  return (
    <span className={className} onMouseEnter={enter} onMouseLeave={leave}>
      {children}
      {anchor && createPortal(
        <div ref={card} className="fixed z-50 pointer-events-none"
          style={{ left: pos?.left ?? -9999, top: pos?.top ?? -9999 }}>
          <Card player={player} profile={profile} />
        </div>,
        document.body,
      )}
    </span>
  );
}
