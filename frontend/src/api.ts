// Thin typed client. Regenerate richer types from /openapi.json when the
// contract grows: npx openapi-typescript http://localhost:8000/openapi.json

async function req<T>(method: string, url: string, body?: unknown): Promise<T> {
  const r = await fetch(url, {
    method,
    credentials: "include",
    headers: body instanceof FormData ? undefined : { "Content-Type": "application/json" },
    body: body instanceof FormData ? body : body ? JSON.stringify(body) : undefined,
  });
  if (r.status === 401) { window.dispatchEvent(new Event("dfs:unauth")); throw new Error("unauthorized"); }
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail ?? r.statusText);
  const ct = r.headers.get("content-type") ?? "";
  return (ct.includes("json") ? r.json() : r.text()) as Promise<T>;
}

export const api = {
  get: <T>(u: string) => req<T>("GET", u),
  post: <T>(u: string, b?: unknown) => req<T>("POST", u, b),
  del: <T>(u: string) => req<T>("DELETE", u),
};

export interface PoolPlayer {
  player_id: number; name: string; position: string; team: string; opponent: string;
  game_key: string; salary: number; status: string | null; dvp_rank: number | null;
  projection: number; floor: number; ceiling: number; stddev: number;
  ownership: number; implied_opp_total: number | null; value: number;
  adjustments: Record<string, number | boolean>;
}
export interface SlateSummary {
  id: number; name: string; draft_group_id: number; season: number | null;
  week: number | null; start_time: string | null; game_count: number;
  pool_version_id: number | null; has_sims: boolean;
}
export interface Evaluation {
  projection: number; salary: number; salary_remaining: number;
  floor: number; median: number; ceiling: number; p95: number; stddev: number;
  histogram: number[]; hist_edges: number[];
  cumulative_ownership: number; product_ownership: number; lineup_type: string;
  marginal: Record<string, number>;
}
export interface Job {
  id: number; kind: string; status: string; progress: number;
  message: string; result: Record<string, unknown>; created_at: string;
}
export interface LineupDTO {
  id: number; ordinal: number; slots: { slot: string; player_id: number | null; name: string | null }[];
  salary: number; projection: number; ceiling: number; ownership: number;
  lineup_type: string; skeleton_key: string;
  evaluation: { floor?: number; median?: number; ceiling?: number; histogram?: number[]; hist_edges?: number[];
                neff_delta?: number | null; expected_payout?: number; roi?: number; p_cash?: number };
  is_draft: boolean;
}

export interface SkeletonStat {
  qb_team: string; opponent: string; game_id: string;
  n_teammates: number; n_bringback: number; dst_with_qb: boolean;
  key: string; display: string; feasible: boolean; salary: number;
  mean: number; ceiling: number; ownership: number;
  teammate_pool: number; bringback_pool: number;
  implied_total: number | null; default_weight: number;
}
export interface SkeletonGame {
  game_id: string; home: string; away: string;
  home_implied: number | null; away_implied: number | null; total: number | null;
}
export interface SkeletonStatsResponse {
  pool_version_id: number; basis: "payout" | "tail"; n_sims_used: number;
  games: SkeletonGame[]; skeletons: SkeletonStat[];
}
export interface SkeletonNeff {
  n_eff: number; n_active: number; basis: string;
  counts: Record<string, number>; contributions: Record<string, number>;
  by_game: Record<string, number>; by_shape: Record<string, number>;
}
export interface ContestRow {
  id: number; contest_key: string; name: string; entry_fee: number | null;
  field_size: number | null; max_entries_per_user: number | null;
  n_entries: number; has_payout_curve: boolean;
}

export function watchJob(id: number, onUpdate: (j: Job) => void): () => void {
  const es = new EventSource(`/api/jobs/${id}/events`);
  es.onmessage = (e) => {
    const j: Job = JSON.parse(e.data);
    onUpdate(j);
    if (["done", "failed", "cancelled"].includes(j.status)) es.close();
  };
  es.onerror = () => es.close();
  return () => es.close();
}
