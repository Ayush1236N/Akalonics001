# AkaLonics — Code Walkthrough

A guided tour of the codebase: what each file owns, how a request flows end to end, and where to
change things. Read this alongside `README.md` (setup) and `MIGRATION.md` (what changed in v2).

Total: ~11,000 lines. Backend 6,400 (Python/FastAPI), frontend 4,600 (React/Vite).

---

## 1. The one-paragraph mental model

A user uploads a file. The backend writes the raw bytes to object storage, then a job parses it into
DataFrames, **profiles** every column (what type is it, how many distinct values, what's the range),
and hands that profile to a **planner** that decides which of 37 chart types the data can actually
support. The planner emits a list of *chart specs* — small JSON objects like
`{type: "bar", x: "region", y: "revenue", agg: "sum"}`. Nothing is rendered yet. The frontend walks
that list, and for each spec asks the backend to **aggregate** just the numbers that spec needs,
then routes the spec to a renderer component. The chart spec is the contract between every layer;
almost every feature is either "produce more specs" or "render a spec better".

---

## 2. Backend

### The request pipeline

```
POST /api/upload
   uploads.py      validate: magic bytes, size, zip-bomb, encrypted-workbook check
   storage.py      persist raw bytes  (LocalStorage | S3Storage)
   jobs.py         enqueue -> returns {job_id} immediately
        │
        └─ worker  profiling.py   parse -> DataFrames -> column profiles
                   framecache.py  write parquet cache, keyed by dataset_id
                   planner.py     profiles -> ranked list of chart specs
                   models.py      persist dataset/sheets/columns/slides rows
        │
GET /api/progress/{job_id}   SSE stream: queued -> parsing -> profiling -> planning -> done
GET /api/datasets/{id}       the profile + the chart plan
POST /api/aggregate          per-chart numbers, on demand
```

### File by file

**Core data path**

| File | Lines | Owns |
|---|---|---|
| `app.py` | 520 | All routes, dependency wiring, middleware, exception handlers. Start here. |
| `uploads.py` | 156 | Upload validation and safety. Magic-byte sniffing, zip-bomb guard, formula/DDE stripping. |
| `profiling.py` | 334 | Parsing (xlsx/csv/tsv/json/docx) and column profiling. Type inference lives here. |
| `planner.py` | 354 | Chart selection rules. Profile in, ranked chart specs out, with the insight sentence. |
| `charting.py` | 614 | The numeric work: groupby, agg, binning, pivots, correlation, LTTB downsampling. |
| `exporters.py` | 569 | PPTX (python-pptx), PDF, XLSX, PNG. Renders charts headless via matplotlib. |
| `nlchart.py` | 329 | Natural-language → chart spec, with validation, one retry, keyword fallback. |
| `datasets.py` | 204 | Dataset CRUD, ownership scoping, the "load a sample" path. |

**Platform**

| File | Lines | Owns |
|---|---|---|
| `config.py` | 112 | Pydantic `Settings`. Every env var is declared here, nowhere else. |
| `db.py` | 93 | Async SQLAlchemy engine and session. Postgres, or SQLite when `DATABASE_URL` is unset. |
| `models.py` | 156 | ORM tables: users, datasets, sheets, columns_meta, presentations, slides, api_keys. |
| `storage.py` | 201 | Storage interface + `LocalStorage` / `S3Storage`. Swap backends via env, no code change. |
| `framecache.py` | 124 | Parquet cache for parsed frames, with TTL sweeping. Replaces v1's in-memory dict. |
| `jobs.py` | 187 | Job queue abstraction. arq/Redis when configured, in-process asyncio otherwise. |
| `worker.py` | 57 | The standalone worker entrypoint for the Redis path. |
| `auth.py` | 197 | Signup, login, refresh rotation, `get_current_user` dependency, guest sessions. |
| `security.py` | 115 | Argon2 hashing, JWT encode/decode, CSRF double-submit tokens. |
| `ratelimit.py` | 37 | slowapi limits: 10 uploads/hr, 100 calls/min, 5 auth/min per IP. |
| `logging_setup.py` | 98 | structlog JSON logs with request IDs; Sentry init (no-op without a DSN). |

**Where to change things**

- *Add a chart type* → add the rule in `planner.py`, the aggregation in `charting.py`, the renderer in the frontend, and the matplotlib version in `exporters.py`.
- *Support a new file format* → `profiling.py` parse dispatch + `uploads.py` allowed types.
- *Change type inference* → `profiling.py`, and update `tests/test_profiling.py` (table-driven).
- *Add an env var* → `config.py` only, plus `.env.example`.

### Auth model

Argon2 password hashes. A 15-minute JWT access token and a rotating 30-day refresh token, both in
httpOnly secure cookies, with a CSRF double-submit token for state-changing calls. `get_current_user`
guards every route except `/health`, `/ready`, and `/auth/*`. **Ownership is enforced by returning
404, not 403**, so you can't probe for the existence of someone else's dataset — `tests/test_ownership.py`
asserts this across ten endpoints.

Guest mode creates a real but ephemeral user, so the sample-file demo works with no signup.

### The NL chart endpoint, and why it's shaped that way

`POST /api/nl-chart` builds a compact schema summary (column names, types, cardinality, sample
values) and asks an LLM for a strict JSON chart spec. The important part is what happens next:
the output is validated against a pydantic model **and** the real column list **and** the 37 known
type names. Invalid output gets one retry with the validation error appended; if that also fails, it
falls back to a keyword matcher with no LLM at all. Model output is never evaluated, and the feature
flags off cleanly when no key is configured. This is why an expired token degrades to a working
keyword-matched chart instead of a 500.

---

## 3. Frontend

```
src/
├── main.jsx        19   mount, providers
├── App.jsx        310   routing, shell, protected routes
├── views/
│   ├── Auth.jsx         155   login / signup / guest
│   ├── Landing.jsx      318   3D hero, drop zone, sample picker
│   ├── Studio.jsx       373   sheet tabs, virtualized table, column type chips
│   ├── Presentation.jsx 399   the deck: slide nav, autoplay, insights, export menu
│   └── Builder.jsx      464   manual chart construction
├── charts/
│   ├── index.jsx        190   ← the dispatcher: spec.type -> component
│   ├── chartjs.jsx      854   ~28 Chart.js types
│   ├── d3charts.jsx     401   sunburst, parallel coords, calendar, sankey
│   ├── three3d.jsx      263   3D scatter / surface / bars (react-three-fiber)
│   └── kpi.jsx           99   stat cards and sparklines
├── components/
│   ├── CommandBar.jsx   194   cmd+K natural-language chart requests
│   ├── Hero3D.jsx       179   cursor-reactive point cloud
│   ├── ErrorBoundary.jsx 49   added after a blank-page crash in QA
│   └── ui.jsx           191   buttons, cards, skeletons, empty states
└── lib/
    ├── api.js           260   fetch client: 401 -> silent refresh -> retry, SSE, CSRF
    ├── auth.jsx          89   auth context
    ├── theme.jsx         57   dark/light
    └── format.js         44   number and date formatting
```

**`charts/index.jsx` is the file to know.** It's a single map from a spec's `type` string to a
renderer, with a fallback that shows the data as a table if a type is unknown. Adding a 38th chart
type is one entry plus one component — you never touch the views.

**Performance choices.** Heavy chart libraries are code-split and dynamically imported per slide, so
the initial bundle doesn't carry Three.js or D3. Charts render to canvas rather than SVG above ~2k
points. Series longer than the cap are downsampled server-side with LTTB, which preserves visual
shape far better than naive sampling. The data table is virtualized. 3D scenes cap device pixel
ratio to hold 60fps.

**`lib/api.js` handles the auth edge case** that bites most apps: when a call 401s, it transparently
refreshes the token and replays the original request once, so an expiring session never surfaces as
a user-visible error.

---

## 4. Tests

- **pytest: 123 tests, 84% backend coverage.** Auth flows, ownership isolation, every upload type,
  table-driven type inference, planner output against fixtures, all aggregate modes, all four export
  endpoints re-parsed to confirm validity, 429 rate limits, malicious-upload rejection.
  `worker.py` is at 0% — the Redis path is untested.
- **vitest: 27 tests.** API client, auth context, chart dispatcher fallback, command bar.
- **`selftest.py`** is the smoke check: renders all 37 types and asserts the planner can produce
  every one of them (`never produced by planner: []`).

```bash
cd backend && pytest              # 123 passed
cd frontend && npm test           # 27 passed
cd backend && python selftest.py  # 37/37 chart types
```

---

## 5. Ops

`Dockerfile` for api and worker (multi-stage, slim, non-root) and a `docker-compose.yml` bringing up
postgres, redis, minio, api, worker, and an nginx-served frontend. Alembic migrations in
`backend/alembic/`. `.github/workflows/ci.yml` runs ruff, pytest, the npm build, and a docker build.
`/health` is liveness; `/ready` checks database, storage, and redis.

**Be aware:** the compose stack, the Postgres migration path, S3Storage, the arq worker, and
Redis-backed rate limiting are unit-tested and written but were **never executed** — this sandbox has
no docker, Postgres, Redis, or MinIO. Those are the things to verify first on real infrastructure.
Everything exercised in QA ran on the SQLite + local-disk + in-process-job fallback path.
