# AkaLonics v1 → v2 migration

v2 is a production-hardening pass. Every v1 capability is intact — all **37 chart types** and all
**4 export formats** (pptx, pdf, xlsx, png) still work, verified by `backend/selftest.py`, the pytest
suite and a browser pass. What changed is everything around them: storage, identity, limits, jobs,
ops and a natural-language chart endpoint.

## Breaking changes

| Area | v1 | v2 |
| --- | --- | --- |
| Dataset storage | `STORE` dict in `app.py`, pickle cache in `backend/.cache` | SQLAlchemy 2.x async ORM (`models.py`) + object storage (`storage.py`) + parquet frame cache (`framecache.py`) |
| Identity | none — any caller could read any `dataset_id` | every `/api/*` route requires a user; datasets carry `owner_id` and foreign datasets return **404** |
| Upload response | full dataset payload (blocking parse) | `{job_id, dataset_id, name, status}`; poll `GET /api/progress/{job_id}` (SSE) then `GET /api/dataset/{id}` |
| Progress SSE | keyed by dataset | keyed by **job id**, emitting real states `queued → parsing → profiling → planning → done/failed` |
| CORS | wildcard | `ALLOWED_ORIGINS` list; wildcard regex only when `DEBUG=true` |
| Upload cap | 60 MB, extension check | `MAX_UPLOAD_MB` (default 25), magic-byte sniffing, zip-bomb guard, formula/DDE stripping, row/col caps, encrypted-workbook rejection |
| Frames in memory | module-level dict of DataFrames | loaded from parquet per request, TTL-swept (`PARQUET_TTL_MIN`) |

Old v1 datasets are **not** migrated: they only ever existed in process memory, so there is nothing
to move. Re-upload (or click a sample) after upgrading.

## New surface

```
POST /api/auth/signup   POST /api/auth/login    POST /api/auth/guest
POST /api/auth/refresh  POST /api/auth/logout   GET  /api/auth/me
POST /api/jobs          GET  /api/jobs/{id}     GET  /api/progress/{job_id}   (SSE)
GET  /api/datasets      GET  /api/dataset/{id}
POST /api/nl-chart      GET  /api/nl-suggestions/{dataset_id}
GET  /api/config        GET  /health · /ready
```

- **Auth**: email + password with argon2 hashing, 15-minute JWT access tokens, rotating 30-day
  refresh tokens stored hashed, httpOnly cookies plus a CSRF double-submit token, and API keys
  (`x-api-key`) for programmatic use. `POST /api/auth/guest` mints an ephemeral user so the sample
  demo still works instantly with no email — this is what the deployed preview uses.
- **NL charts**: `POST /api/nl-chart` builds a compact schema summary, asks the model for a strict
  JSON chart spec, validates it against a pydantic model *and* the real column list *and* the 37
  supported type names, retries once with the validation error, then falls back to a keyword matcher.
  Model output is never `eval`'d (a test greps for `eval(`/`exec(`/`subprocess`/`pickle.loads`).
  With no `ANTHROPIC_API_KEY` the flag reports off and the keyword matcher answers everything.

## New backend modules

`config.py` (pydantic Settings) · `db.py` (async engine/session, SQLite WAL pragmas) ·
`models.py` (users, api_keys, refresh_tokens, datasets, sheets, columns_meta, presentations,
slides, jobs) · `storage.py` (`LocalStorage` / `S3Storage`) · `framecache.py` (parquet + TTL sweeper) ·
`datasets.py` (ownership-scoped service layer) · `auth.py` · `security.py` · `uploads.py` (safety
checks) · `jobs.py` (arq when `REDIS_URL` + `USE_WORKER`, in-process asyncio fallback otherwise) ·
`worker.py` · `ratelimit.py` (slowapi) · `logging_setup.py` (structlog + request IDs) ·
`nlchart.py` · `alembic/` · `tests/`.

## Frontend changes

- `lib/auth.jsx` — auth context (`loading | anon | authed`), feature flags from `/api/config`.
- `views/Auth.jsx` — login / signup / continue-as-guest.
- `lib/api.js` — rewritten: bearer + CSRF headers, 401 → one silent refresh → retry, signed URLs for
  SSE and downloads, job-based progress.
- `components/CommandBar.jsx` — NL command bar in Builder and Presentation: suggestion chips,
  debounce, AbortController cancel, cmd+K, inline error, model-vs-keyword provenance note.
- `components/ErrorBoundary.jsx` — a render failure shows a recoverable panel instead of a blank page.

## Upgrade steps

1. `pip install -r backend/requirements.txt` (adds sqlalchemy, aiosqlite, asyncpg, alembic, pyarrow,
   argon2-cffi, pyjwt, slowapi, arq, structlog, boto3, anthropic).
2. Copy `backend/.env.example` to `backend/.env`. Zero-infra dev needs no edits; production must set
   `JWT_SECRET`, `DATABASE_URL`, `ALLOWED_ORIGINS`, `DEBUG=false`, `COOKIE_SECURE=true`.
3. Postgres: `cd backend && alembic upgrade head`. On SQLite the tables are created at startup.
4. `cd frontend && npm install && npm run build`.
