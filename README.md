# AkaLonics — chart studio + Demand Forecasting

Upload a spreadsheet, CSV, JSON or Word report. AkaLonics profiles every column server-side, decides
which of its **37 chart types** the data actually supports, writes an insight sentence for each, and
plays the whole thing back as an animated presentation deck. Ask for a chart in plain English
("average revenue per channel as a horizontal bar") and it is validated, built and appended to the
deck. Everything exports as PPTX, PDF, XLSX or per-slide PNG.

Alongside the chart studio sits **Demand Forecasting** — compare seven forecasting models side by
side across three inventory categories (T7-F / T7-SFG / T7-R) at five time granularities, with a
Future Demand table of every model's upcoming-period predictions. See
[`FORECASTING.md`](FORECASTING.md) for the full guide.

v2 adds accounts, per-user isolation, real persistence, background jobs, rate limits, upload safety,
containers and a test suite. See [`MIGRATION.md`](MIGRATION.md) for what changed from v1.

## Architecture

```
                       ┌──────────────────────────────────────────┐
  browser              │ React + Vite (frontend/)                 │
  ───────────────────► │  Auth · Landing · Studio · Presentation  │
                       │  Builder · CommandBar (cmd+K)            │
                       └───────────────┬──────────────────────────┘
                          JSON + SSE   │  bearer JWT + CSRF, 401 → silent refresh
                       ┌───────────────▼──────────────────────────┐
                       │ FastAPI (backend/app.py)                 │
                       │  auth.py  ratelimit.py  logging_setup.py │
                       │  uploads.py (magic bytes, zip bomb, DDE) │
                       │  datasets.py — ownership-scoped services │
                       │  profiling · charting · planner · nlchart│
                       │  forecasting.py · forecast_service.py    │
                       │  (Demand Forecasting — T7, DB-backed)    │
                       │  exporters.py (pptx/pdf/xlsx/png)        │
                       └───┬──────────┬──────────┬────────────────┘
                           │          │          │
              ┌────────────▼──┐  ┌────▼──────┐  ┌▼───────────────┐
              │ SQLAlchemy 2  │  │ storage.py│  │ jobs.py        │
              │ Postgres      │  │ S3/MinIO  │  │ arq + Redis    │
              │ ─ or ─ SQLite │  │ ─ or ─    │  │ ─ or ─         │
              │ (zero infra)  │  │ local disk│  │ in-process     │
              └───────────────┘  └───────────┘  └────────────────┘
                                       │
                            framecache.py — parquet frames, TTL swept
```

Each dotted alternative is chosen by environment: unset `DATABASE_URL` → SQLite, unset
`STORAGE_BACKEND=s3` → local disk, unset `REDIS_URL`/`USE_WORKER` → in-process asyncio jobs. Nothing
extra needs to run for local development.

## Quickstart A — zero infrastructure

No Postgres, Redis or MinIO. SQLite + local disk + in-process jobs.

```bash
cd backend
pip install -r requirements.txt
python -m uvicorn app:app --port 8000        # tables are created at startup

cd ../frontend
npm install
npm run dev            # http://localhost:5173, proxies /api to :8000
```

Open the app, click **continue as guest** (or sign up), then **try a sample** — no email needed.

## Quickstart B — full stack with Docker

```bash
cp backend/.env.example backend/.env     # optional; compose sets its own env
docker compose up --build
# api      → http://localhost:8000       (postgres + redis + minio behind it)
# frontend → http://localhost:8080       (nginx serving the built bundle)
# minio    → http://localhost:9001       (console, minioadmin/minioadmin)
```

Compose brings up `postgres:16`, `redis:7`, `minio`, `api` (uvicorn, non-root), `worker`
(`python worker.py`, arq) and `frontend` (nginx). The api entrypoint runs `alembic upgrade head`
before serving. `docker compose down -v` clears volumes.

> Not verified in this sandbox: the container images and compose file were authored and syntax-checked
> here, but neither `docker` nor `docker compose` is installed in the build environment, so
> `docker compose up` has not been executed. Treat Quickstart B as reviewed-not-run.

## Environment variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `DEBUG` | `true` | `false` locks CORS to `ALLOWED_ORIGINS` |
| `LOG_LEVEL` | `INFO` | structlog level |
| `DATABASE_URL` | — | unset ⇒ SQLite at `backend/.data/akalonics.db`; `postgres://`, `postgresql://` and `sqlite:///` are rewritten to async drivers |
| `REDIS_URL` | — | needed for arq jobs and shared rate-limit state |
| `STORAGE_BACKEND` | `local` | `local` \| `s3` |
| `STORAGE_DIR` | `.data/objects` | local object root |
| `S3_BUCKET`, `S3_ENDPOINT_URL`, `S3_REGION`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY` | — | S3/MinIO credentials |
| `JWT_SECRET` | dev placeholder | **set in production** (32+ random bytes) |
| `ACCESS_TTL_MIN` / `REFRESH_TTL_DAYS` | `15` / `30` | token lifetimes |
| `COOKIE_SECURE` / `COOKIE_SAMESITE` | `false` / `none` | `true` / `lax` behind HTTPS on one origin |
| `ALLOWED_ORIGINS` | `localhost:5173,localhost:4173` | comma list |
| `MAX_UPLOAD_MB` | `25` | upload cap |
| `RATE_LIMIT_ENABLED` | `true` | master switch |
| `RATE_UPLOADS` / `RATE_API` / `RATE_AUTH` | `10/hour` / `100/minute` / `5/minute` | per user, per user, per IP |
| `MAX_ROWS` / `MAX_COLS` | `500000` / `512` | parse caps |
| `MAX_ZIP_MEMBERS` / `MAX_UNCOMPRESSED_MB` | `2000` / `400` | xlsx zip-bomb guard |
| `PARQUET_TTL_MIN` | `180` | frame cache TTL |
| `USE_WORKER` | `false` | `true` + `REDIS_URL` ⇒ arq worker handles uploads |
| `SENTRY_DSN` | — | no-op when unset |
| `GUEST_MODE` | `true` | `false` ⇒ `/api/auth/guest` returns 403 |
| `NL_CHARTS_ENABLED` / `NL_MODEL` | `true` / `claude_sonnet_4_6` | NL charts; without `ANTHROPIC_API_KEY` the keyword matcher answers everything |

Full annotated template: `backend/.env.example`.

## API

```
public   GET  /health · /ready · /api/config · /api/chart-types · /api/samples
auth     POST /api/auth/signup · login · guest · refresh · logout    GET /api/auth/me
data     POST /api/upload → {job_id, dataset_id}      POST /api/sample/{filename}
         POST /api/jobs   GET /api/jobs/{id}           GET /api/progress/{job_id}  (SSE)
         GET  /api/datasets · /api/dataset/{id} · /api/rows/{id}
charts   POST /api/analyze · /api/aggregate · /api/describe
         POST /api/nl-chart      GET /api/nl-suggestions/{dataset_id}
export   GET  /api/export/{dataset_id}?sheet=…&format=pptx|pdf|xlsx
         GET  /api/export/png/{dataset_id}/{slide_id}?sheet=…
forecast GET  /api/forecast/meta                      GET/POST/DELETE /api/forecast/data/{category}
         POST /api/forecast/sample/{category}         POST /api/forecast/bundled/{name}
         POST /api/forecast/run
```

Everything under `/api` except the public list requires a session (guests included). Reads are
scoped by `owner_id`; another user's `dataset_id` returns **404**, asserted by
`tests/test_ownership.py`. All aggregation happens in pandas, so the browser preview and the exported
file are computed from the same numbers.

## Tests

```bash
cd backend && pytest                  # 123 passed · 84% statement coverage
cd backend && python selftest.py      # smoke: all 37 chart types + 4 exports on every sample
cd frontend && npm run test:run       # 26 passed (api client, auth context, charts, command bar)
cd frontend && npx oxlint src         # 0 errors
```

`pytest` covers auth flows and argon2 hashing, CSRF double-submit, a structural check that every
`/api` route is guarded, ownership isolation across every read path, upload of each file type through
the job flow, malicious-upload rejection (PDF magic bytes, ELF, zip bomb, encrypted workbook,
oversize), profiling inference, the planner, all six aggregation modes, all 37 chart builders, all
four export formats parsed back with python-pptx/openpyxl/PDF magic, NL-chart validation + retry +
keyword fallback + a no-eval guard, rate-limit 429s, and health/ready.

## Deploy notes

- **Preview (sandbox)**: start the API with `start_server` on port 8000, `npm run build`, then
  `deploy_website(project_path="frontend/dist", site_name="AkaLonics", entry_point="index.html")`.
  The bundle calls `__PORT_8000__`, a placeholder the hosting proxy rewrites to the sandbox backend;
  it falls back to `http://localhost:8000` locally, so the same `dist/` works in both places.
- **Production**: `DEBUG=false`, real `JWT_SECRET`, `ALLOWED_ORIGINS` set to your frontend origin,
  `COOKIE_SECURE=true`, `COOKIE_SAMESITE=lax` when frontend and API share a site, Postgres +
  `alembic upgrade head`, `STORAGE_BACKEND=s3`, `REDIS_URL` + `USE_WORKER=true` and at least one
  `python worker.py` process.
- Because the sandbox preview is cross-site, the frontend also keeps the session in memory +
  `sessionStorage` and sends a bearer token, so it works where third-party cookies are blocked.
- CI (`.github/workflows/ci.yml`): ruff, pytest with coverage, selftest, vitest, npm build,
  docker build.

## Chart types (37)

**KPI** kpi · **Trend** line, area, multi_line, stacked_area, step_line, candlestick, calendar ·
**Comparison** bar, horizontal_bar, grouped_bar, stacked_bar, pareto, waterfall, funnel ·
**Composition** pie, donut, gauge, treemap, sunburst, radar, polar_area, radial_bar, sankey ·
**Relationship** scatter, regression, bubble, heatmap, parallel ·
**Distribution** histogram, kde, box, violin · **Text** wordcloud ·
**3D** scatter3d, surface3d, bar3d

Sample decks: `sales.xlsx` → 36 slides, `survey.csv` → 37 slides, `report.docx` → 23 slides.
Regenerate samples with `python backend/make_samples.py`.

## Frontend layout

- `views/Auth.jsx` — login, signup, continue as guest
- `views/Landing.jsx` — 3D hero (Three.js point cloud), dropzone, sample cards, live job progress
- `views/Studio.jsx` — column profile cards with editable roles, searchable paginated row table
- `views/Presentation.jsx` — deck playback: keyboard nav, autoplay, overview grid, per-slide table, exports, Ask bar
- `views/Builder.jsx` — pick any chart type and map fields manually, or describe it
- `views/DemandForecasting.jsx` — Demand Forecasting: T7 category tabs, granularity toggle, SKU
  drill-down, manual/bundled/synthetic data entry, sortable metrics table, the Future Demand table
  (all 7 models' upcoming values side by side), and the per-model Line/Bar/Pie + shared 3D surface panel
- `charts/` — `chartjs.jsx` (28 types), `d3charts.jsx` (sunburst, parallel coords, funnel, calendar, radial bar), `three3d.jsx` (3D scatter/surface/bars), `kpi.jsx`, `index.jsx` (lazy dispatcher, per-chart error boundary, table fallback)

## Known limitations

- `docker compose up` is unverified here (no Docker in the build sandbox); Postgres, Redis, MinIO, S3
  and the arq worker code paths are exercised by unit tests and configuration checks, not by a live
  service.
- The sandboxed preview iframe blocks `localStorage`, so theme choice does not persist across reloads
  in the preview (it does on a normal origin); sessions use `sessionStorage` + in-memory tokens.
- Guest sessions are ephemeral users: signing out or clearing the tab drops access to their datasets.
- Frames that pyarrow refuses (exotic mixed-type columns) fall back to a pickle cache entry.
- The D3 calendar heatmap renders at its natural size rather than stretching to the slide.
- On 3D slides the third axis label can pass behind the geometry depending on orbit angle.
- Stacked-bar x-axis category labels truncate at 12 characters.
- The `react-three-fiber` chunk is ~880 KB, lazy-loaded only for 3D slides.
- `.xls` support depends on the environment providing `xlrd`; `.xlsx` is the tested path.
