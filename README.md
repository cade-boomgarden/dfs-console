# DFS Console

Simulation-first DraftKings NFL Classic optimizer. The sims matrix
`sims[n_sims, n_players]` is the primary data structure; everything —
evaluation, selection, N_eff, the field model to come — reads from it.

Built to `OPTIMIZER_REQUIREMENTS.md`, covering build-order items 0–11 end to
end, plus skeleton enumeration/allocation (17, seeded into Stage A), the
two-stage build (18–19 with expected score standing in for expected payout
until the field sampler lands), and the N_eff gate.

## Quick start (dev, no Docker)

```bash
pip install -e ".[dev]"
cp .env.example .env                      # fill in secrets; NEVER commit .env
python -m backend.auth.seed cade          # seed a user (prompts for password)
uvicorn backend.main:app --reload         # API on :8000, SQLite, thread jobs

cd frontend && npm install && npm run dev # UI on :5173, proxied to :8000
```

Ingest the captured fixtures for a working offline slate: on the Slates page,
"Ingest fixture slate" (or POST `/api/slates/ingest` with
`{"fixture_dir": "backend/tests/fixtures"}`), then Simulate on the Overview
page, then build.

## Production

Deployed on **Render** as a single service — the API serves the built React
bundle, so there is no separate frontend host and no CORS in production.
`render.yaml` declares the web service, Postgres, and the sims disk.
See **[DEPLOY.md](DEPLOY.md)** for the full walkthrough.

`docker-compose.yml` runs postgres + redis + api + rq worker locally if you
want to exercise the RQ path; the deploy does not use it. Blobs go through the
`BlobStore` protocol — `LocalBlobStore` on a mounted disk today, an
S3-compatible store (Cloudflare R2) when a separate worker needs to share
them.

## Layout

```
backend/core/       PURE — imports nothing from sources/jobs/models/api
  solver.py         CP-SAT lineup solver (tested, unchanged)
  variance.py       component-level player simulation (tested, unchanged)
  scoring.py        DK constants + DST step function (simulated, never lookup)
  sims.py           slate sims matrix; per-game RNG partitioning; int16 pack
  evaluator.py      evaluate(lineup, sims); portfolio scores; N_eff
  validator.py      hand-builder legality (shares RosterRules with solver)
  skeletons.py      enumeration + allocation (the generator-spread lever)
backend/sources/    one adapter per source; golden-file tested parsers
backend/identity/   canonical crosswalk; confidence scores; review queue
backend/jobs/       ingest / simulate / build / export / results; thread or RQ
backend/api/        FastAPI routers; SSE job progress
frontend/           Vite + React + TS + Tailwind; dark; tabular numerals
```

## Weekly workflow

1. **Ingest** (Slates page) — resolves the main slate (15a filter), pulls
   draftables + FP + odds, merges into an immutable pool version.
2. **Simulate** (Overview) — builds the sims matrix; floors/ceilings on the
   pool grid come from it.
3. **Adjust** (Pool) — locks, fades, deltas, multipliers; persisted with
   provenance.
4. **Build** (Builds) — skeleton-seeded Stage A, top-N-unique Stage B,
   N_eff on every run.
5. **Hand-build** (Builder) — live validation + sub-100ms evaluation against
   the resident sims matrix; optimizer-assisted completion.
6. **Import DKEntries → assign → export** (Contests).
7. **After the slate: import standings** — results, ROI, and the realised
   ownership that calibrates the ownership model. Archive every week.

## Tests

```bash
python -m pytest backend/tests
```

Golden-file parser tests run against captured real API payloads in
`backend/tests/fixtures/`; recapture on the first live pull after any
provider change and commit the new fixtures.
