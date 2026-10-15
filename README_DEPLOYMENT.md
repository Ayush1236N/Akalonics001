# Deploying AkaLonics

This repo is a full-stack app: a FastAPI backend (auth, Postgres, the 37-chart
studio, and the Demand Forecasting / T7 module) and a React + Vite frontend.
`render.yaml` at the repo root is a **Render Blueprint** — it describes all
three pieces (database, backend, frontend) so Render can stand the whole
thing up from one file. This doc walks through that path, then covers local
development and the things worth upgrading once you're past a first deploy.

## 0. What you need before starting

- A GitHub account, with this code pushed to a repo (empty repo is fine —
  see step 1).
- A free [Render](https://render.com) account (sign up with GitHub — it
  makes step 2 a one-click "connect repo").

Nothing else. The blueprint provisions its own Postgres database; you don't
need to set one up separately.

## 1. Push this code to GitHub

```bash
cd akalonics
git init
git add .
git commit -m "AkaLonics — chart studio + Demand Forecasting"
git branch -M main
git remote add origin https://github.com/<your-username>/<your-repo>.git
git push -u origin main
```

(If you already created a repo on GitHub, skip `git init` and just add it as
the remote.)

## 2. Deploy the Blueprint on Render

1. Go to the [Render dashboard](https://dashboard.render.com) → **New +** →
   **Blueprint**.
2. Connect your GitHub account if you haven't already, then pick the repo
   you just pushed.
3. Render finds `render.yaml` and shows you three resources it's about to
   create: **akalonics-db** (Postgres), **akalonics-api** (the backend,
   built from `backend/Dockerfile`), and **akalonics-frontend** (the static
   React build). Click **Apply**.
4. First build takes a few minutes — the backend image installs Python deps,
   the frontend runs `npm install && npm run build`. Watch both build logs;
   Render will show ✅ once each is live.

## 3. One thing to double-check after the first deploy

`render.yaml` assumes the service names `akalonics-api` and
`akalonics-frontend` aren't already taken by someone else on Render (URLs
are global: `https://<name>.onrender.com`). If Render had to pick a
different name for either service (you'll see it in the dashboard header),
do this once:

1. Open **akalonics-frontend → Environment**, set `VITE_API_BASE` to the
   *actual* backend URL, save. This triggers a rebuild.
2. Open **akalonics-api → Environment**, set `ALLOWED_ORIGINS` to the
   *actual* frontend URL, save. This triggers a redeploy.

If the names weren't taken, both are already correct and you can skip this.

## 4. Try it

Open the frontend URL. You'll land on the sign-up/guest screen (auth is on
by default — see §7 if you'd rather not require login). Once in:

- **Demand Forecasting** (top nav) → pick a T7 category → **Manage demand
  data** → load either the synthetic sample or one of the two bundled real
  datasets (your original Excel files, converted to seed data) → the seven
  models, the metrics table, the new **Future Demand** table, and the chart
  panel all populate.
- The regular chart studio (upload a spreadsheet/CSV/JSON/Word file) works
  the same way it always did.

## 5. Local development

Two options:

**Docker Compose (closest to production, needs Docker installed):**

```bash
docker compose up --build
# frontend: http://localhost:8080
# backend:  http://localhost:8000
```

This also brings up Postgres, Redis and MinIO locally, matching the full
production topology.

**Plain Python + Node (faster iteration, zero-infra dev mode):**

```bash
# backend — SQLite, no Postgres/Redis needed
cd backend
pip install -r requirements.txt
python app.py            # http://localhost:8000

# frontend — separate terminal
cd frontend
npm install
npm run dev               # http://localhost:5173, proxies /api to :8000
```

## 6. Where things live

- **Demand Forecasting data** (the historical rows you enter or load per
  T7-F / T7-SFG / T7-R category) is stored in Postgres, scoped to your user
  account — it survives redeploys and restarts regardless of plan.
- **Chart-studio uploads** (spreadsheets you drag in for the 37-chart-type
  studio) are cached to local disk on the backend service. On Render's
  **free** plan that disk is ephemeral — an upload can disappear if the
  service restarts. This doesn't affect Demand Forecasting. To make uploads
  durable too, either upgrade the backend to a paid plan and add a Render
  persistent disk (mount path `/data`), or switch `STORAGE_BACKEND` to `s3`
  and point it at any S3-compatible bucket (Backblaze B2, Cloudflare R2,
  AWS S3 all work — see `backend/.env.example` for the variable names).

## 7. Things worth changing before real production use

- **Guest mode**: `GUEST_MODE=true` (the default here) lets anyone hit
  "continue as guest" with no signup. Set it to `false` in
  **akalonics-api → Environment** once you only want registered accounts.
- **Free plan cold starts**: Render's free web services sleep after 15
  minutes idle; the first request after that takes 30–60s to wake up.
  Upgrade `akalonics-api` (and optionally `akalonics-frontend`, though
  static sites don't sleep) to the **Starter** plan to remove this.
- **NL command bar** ("ask for a chart in plain English") is off by default
  (`NL_CHARTS_ENABLED=false`) since it needs an Anthropic API key. To turn
  it on: set `NL_CHARTS_ENABLED=true` and add `ANTHROPIC_API_KEY` in
  **akalonics-api → Environment**.
- **Database migrations**: the app auto-creates tables on first boot
  (convenient for this first deploy) but the repo also ships real Alembic
  migrations under `backend/alembic/`. Once you're iterating on the schema,
  run `alembic upgrade head` (via Render's shell, or a one-off job) instead
  of relying on auto-create.
- **Rate limits / worker / background jobs**: already wired up
  (`slowapi`, `arq`) but the worker service isn't part of this Blueprint to
  keep the first deploy to two services. `docker-compose.yml` shows the
  full topology (`worker`, `redis`, `minio`) if you want to add them as
  Render private services later.

## 8. Support

- `README.md` — architecture, API surface, chart types.
- `FORECASTING.md` — Demand Forecasting data schema, the seven models, the
  chart panel, and how to extend it (new model / category / chart type).
- `WALKTHROUGH.md` / `MIGRATION.md` — how the app got to this v2 shape.
