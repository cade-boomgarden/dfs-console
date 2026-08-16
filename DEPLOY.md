# Deploying to Railway

The app deploys as **one service**. `backend/main.py` serves the built React
bundle itself, so there is no separate frontend host, no CORS in production,
and no cross-origin cookie problem. The Dockerfile builds the frontend in a
Node stage and copies `dist/` into the Python image.

Total moving parts: **1 service + 1 Postgres**. That's it to start.

## 1. Push to GitHub

```bash
git remote add origin git@github.com:<you>/dfs-console.git
git branch -M main
git push -u origin main
```

The repo is already initialized with a first commit. `.env` and `blobs/` are
gitignored — keep it that way.

## 2. Create the Railway project

1. railway.app → **New Project** → **Deploy from GitHub repo** → pick the repo.
2. Railway reads `railway.json`, sees `builder: DOCKERFILE`, and builds. The
   first build takes ~3–5 min (npm install + pip install ortools).
3. In the same project: **New** → **Database** → **Add PostgreSQL**.

## 3. Variables

On the **app service** → Variables:

| Variable | Value |
|---|---|
| `DFS_DATABASE_URL` | `${{Postgres.DATABASE_URL}}` (Railway reference, not a literal) |
| `DFS_SESSION_SECRET` | `openssl rand -hex 32` output |
| `DFS_ENV` | `prod` (makes the session cookie HTTPS-only) |
| `DFS_JOB_MODE` | `thread` |
| `DFS_BLOB_DIR` | `/data/blobs` |
| `DFS_ODDS_API_KEY` | your **rotated** key |
| `DFS_FANTASYPROS_API_KEY` | your key |
| `DFS_BOOTSTRAP_USER` | `cade` |
| `DFS_BOOTSTRAP_PASSWORD` | a strong password |

`${{Postgres.DATABASE_URL}}` is Railway's variable-reference syntax — type it
literally and Railway substitutes the internal connection string at deploy.

## 4. Volume for the sims matrix

Service → **Settings** → **Volumes** → **Add Volume**, mount path `/data`,
1 GB. This is why `DFS_BLOB_DIR=/data/blobs`: sims blobs are ~11 MB each at
50k sims and must survive redeploys. Without a volume the container filesystem
resets on every deploy and you re-simulate every time.

## 5. Deploy and log in

Redeploy after setting variables. Then:

1. Settings → Networking → **Generate Domain**. You get
   `something.up.railway.app`.
2. Open it, log in with the bootstrap credentials.
3. **Delete `DFS_BOOTSTRAP_USER` and `DFS_BOOTSTRAP_PASSWORD`** from Variables
   and redeploy. They exist only so a fresh deploy needs no shell access.

Add the other two accounts with `railway ssh` → `python -m backend.auth.seed
<username>`, or temporarily re-set the bootstrap vars.

Migrations run automatically: the container's start command is
`alembic upgrade head && uvicorn ...`.

## 6. Verify

- `https://<domain>/api/health` → `{"ok": true}`
- Slates → **Ingest fixture slate** → Simulate → build 5 lineups. This exercises
  the whole path against committed fixtures without spending an API credit.
- Then ingest the live slate.

## When to add the worker (not yet)

`DFS_JOB_MODE=thread` runs builds inside the API process. For three users and
a build every few days that is fine — CP-SAT releases the GIL, so request
handling stays responsive. Add a worker when a 150-lineup build makes the UI
sluggish:

1. New service, same repo, start command `python -m backend.worker`.
2. Add Railway Redis; set `DFS_REDIS_URL=${{Redis.REDIS_URL}}` on **both**
   services and `DFS_JOB_MODE=rq` on the API.
3. **A Railway volume can only mount to one service.** Once a worker writes
   sims that the API reads, move blobs to object storage: implement the
   `BlobStore` protocol in `backend/storage/` against Cloudflare R2 (S3 API,
   no egress fees) and point both services at it. `LocalBlobStore` is ~40 lines;
   the R2 version is about the same.

Do not skip ahead to this. The single service is the correct starting shape.

## Scheduled pulls

`backend/scheduler.py` holds `PULL_SCHEDULE` (10 weekly pulls, America/Chicago).
Railway cron jobs are a separate service with a start command and a cron
expression; the container runs, exits, and you are billed for the seconds used.
Wire each pull to a small `python -m backend.jobs.<kind>` entrypoint when you
want it automated. Manual ingest from the UI is fine until then.

## Cost

Hobby plan is $5/mo including $5 of usage. One always-on small service plus
Postgres plus a 1 GB volume lands in the $5–15/mo range depending on how much
the service actually runs. Check current Railway pricing — it changes.

## If you'd rather not use Railway

Render and Fly.io both deploy this same Dockerfile with the same single-service
shape. Render's free Postgres expires after 90 days; Fly needs a `fly.toml` and
`fly volumes create`. Railway is the least ceremony of the three for this.
