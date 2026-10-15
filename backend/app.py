"""AkaLonics API — parse, profile, plan and export chart presentations.

v2: Postgres/SQLite persistence, auth, object storage, async jobs, NL charts.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

import pandas as pd
from fastapi import (APIRouter, Body, Depends, FastAPI, File, HTTPException, Query,
                     Request, Response, UploadFile)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

import charting
import datasets as ds_service
import forecast_samples
import forecast_service
import forecasting
import framecache
import jobs as job_service
import nlchart
import planner
import profiling
from auth import get_current_user, router as auth_router
from config import settings
from db import get_session, init_models, ping as db_ping, reset_engine, session_scope
from exporters import build_pdf, build_pptx, build_xlsx, render_slide
from logging_setup import RequestIDMiddleware, configure_logging, get_logger, init_sentry
from models import Dataset, User
from profiling import ParseError
from ratelimit import limiter, rate_limit_handler
from storage import get_storage

BASE = Path(__file__).resolve().parent
SAMPLES = BASE.parent / "samples"
log = get_logger("akalonics.api")


def _err(status: int, msg: str):
    raise HTTPException(status_code=status, detail=msg)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    init_sentry()
    await init_models()
    sweeper = asyncio.create_task(framecache.sweeper_loop())
    log.info("startup", database="sqlite" if settings.is_sqlite else "postgres",
             storage=settings.storage_backend, worker=job_service.worker_enabled(),
             nl_charts=nlchart.llm_enabled())
    try:
        yield
    finally:
        sweeper.cancel()
        await job_service.drain()
        await reset_engine()


app = FastAPI(title=settings.app_name, version="2.0", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, rate_limit_handler)
app.add_middleware(SlowAPIMiddleware)
app.add_middleware(RequestIDMiddleware)

if settings.debug:
    # Dev + sandbox preview: any origin, credentials allowed (regex, not wildcard).
    app.add_middleware(CORSMiddleware, allow_origin_regex=".*", allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"],
                       expose_headers=["Content-Disposition", "X-Request-ID"])
else:
    app.add_middleware(CORSMiddleware, allow_origins=settings.origins, allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"],
                       expose_headers=["Content-Disposition", "X-Request-ID"])

app.include_router(auth_router)

# Everything below requires an authenticated user (guest users count).
api = APIRouter(prefix="/api", dependencies=[Depends(get_current_user)])


# --------------------------------------------------------------------------- #
# public: liveness / readiness / feature flags
# --------------------------------------------------------------------------- #
@app.get("/api/health")
@app.get("/health")
async def health():
    return {"ok": True, "version": "2.0", "charts": len(charting.BUILDERS)}


@app.get("/api/ready")
@app.get("/ready")
async def ready():
    checks = {"database": await db_ping(),
              "storage": await asyncio.to_thread(get_storage().healthy),
              "redis": True}
    if settings.redis_url:
        try:
            import redis.asyncio as aioredis
            client = aioredis.from_url(settings.redis_url)
            checks["redis"] = bool(await client.ping())
            await client.aclose()
        except Exception:  # noqa: BLE001
            checks["redis"] = False
    ok = all(checks.values())
    return Response(content=json.dumps({"ok": ok, "checks": checks}),
                    media_type="application/json", status_code=200 if ok else 503)


@app.get("/api/config")
async def public_config():
    return {"nl_charts": nlchart.llm_enabled(),
            "nl_charts_ui": settings.nl_charts_enabled,
            "guest_mode": settings.guest_mode,
            "llm_model": settings.nl_model if nlchart.llm_enabled() else None,
            "max_upload_mb": settings.max_upload_mb,
            "worker": job_service.worker_enabled(),
            "chart_types": len(charting.BUILDERS)}


# --------------------------------------------------------------------------- #
# metadata
# --------------------------------------------------------------------------- #
@api.get("/chart-types")
async def chart_types():
    return {"types": [{"id": k, "name": v[0], "description": v[1]}
                      for k, v in charting.CHART_META.items()]}


SAMPLE_META = {
    "sales.xlsx": ("Retail sales workbook", "Three sheets: transactions, targets and store roster — "
                                            "dates, categories and revenue."),
    "survey.csv": ("Product survey", "1,200 responses with satisfaction scores, segments and free text."),
    "report.docx": ("Quarterly report", "Word document with tables plus numeric statements in prose."),
}


@api.get("/samples")
async def samples():
    out = []
    for f in sorted(SAMPLES.glob("*")):
        if f.suffix.lower() in profiling.ALLOWED_EXT:
            title, desc = SAMPLE_META.get(f.name, (f.stem.title(), "Sample dataset"))
            out.append({"file": f.name, "title": title, "description": desc,
                        "size": f.stat().st_size, "ext": f.suffix.lstrip(".")})
    return {"samples": out}


@api.get("/datasets")
async def list_datasets(user: User = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(
        select(Dataset).where(Dataset.owner_id == user.id).order_by(Dataset.created_at.desc())
        .limit(50))).scalars().all()
    return {"datasets": [{"dataset_id": d.id, "name": d.name, "status": d.status,
                          "created_at": d.created_at.isoformat() if d.created_at else None}
                         for d in rows]}


@api.get("/datasets/{dataset_id}")
async def get_dataset(dataset_id: str, user: User = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    ds = await ds_service.require_dataset(session, dataset_id, user.id)
    if ds.status == "failed":
        _err(422, ds.error or "That upload failed to parse.")
    return await ds_service.dataset_payload(session, ds)


# --------------------------------------------------------------------------- #
# ingest
# --------------------------------------------------------------------------- #
@api.post("/upload")
@limiter.limit(settings.rate_uploads)
async def upload(request: Request, file: UploadFile = File(...),
                 user: User = Depends(get_current_user),
                 session: AsyncSession = Depends(get_session)):
    """Stores the raw file and queues parse+profile+plan. Returns a job id at once."""
    raw = await file.read()
    limit = settings.max_upload_mb * 1024 * 1024
    if len(raw) > limit:
        _err(413, f"File is larger than the {settings.max_upload_mb} MB limit.")
    filename = file.filename or "upload"
    try:
        import uploads as upload_guard
        upload_guard.sniff(filename, raw)  # fail fast on unsafe bytes
    except ParseError as exc:
        _err(422, str(exc))
    ds = await ds_service.create_dataset(session, user.id, filename, filename, raw,
                                         status="pending")
    job = await job_service.create_job(session, user.id, "ingest", ds.id)
    await session.commit()
    await job_service.enqueue(job.id, "ingest")
    return {"job_id": job.id, "dataset_id": ds.id, "name": ds.name, "status": "queued"}


@api.post("/sample/{filename}")
async def load_sample(filename: str, user: User = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    """Synchronous so the guest demo is instant."""
    path = (SAMPLES / filename).resolve()
    if not str(path).startswith(str(SAMPLES.resolve())) or not path.exists():
        _err(404, f"Sample '{filename}' not found.")
    try:
        ds = await ds_service.ingest_upload(session, user.id, path.name, path.read_bytes())
    except ParseError as exc:
        _err(422, str(exc))
    return await ds_service.dataset_payload(session, ds)


# --------------------------------------------------------------------------- #
# reads
# --------------------------------------------------------------------------- #
@api.get("/rows/{dataset_id}")
async def rows(dataset_id: str, sheet: str | None = None, offset: int = 0, limit: int = 200,
               sort: str | None = None, desc: bool = False, q: str | None = None,
               user: User = Depends(get_current_user),
               session: AsyncSession = Depends(get_session)):
    _ds, row, df, _types = await ds_service.get_sheet(session, dataset_id, user.id, sheet)
    if q:
        mask = df.apply(lambda col: col.astype(str).str.contains(q, case=False, na=False)).any(axis=1)
        df = df[mask]
    if sort and sort in df.columns:
        df = df.sort_values(sort, ascending=not desc, kind="stable")
    payload = profiling.rows_payload(df, max(0, offset), min(500, max(1, limit)))
    payload["sheet"] = row.name
    return payload


@api.post("/analyze")
async def analyze(payload: dict = Body(...), user: User = Depends(get_current_user),
                  session: AsyncSession = Depends(get_session)):
    dsid = payload.get("dataset_id")
    if not dsid:
        _err(400, "dataset_id is required.")
    ds, row, df, types = await ds_service.get_sheet(session, dsid, user.id, payload.get("sheet"))
    override = payload.get("types") or {}
    merged = {**types, **{k: v for k, v in override.items() if k in types}}
    limit = int(payload.get("limit") or 40)
    cached = [] if override else await ds_service.get_plan(session, ds, row.name)
    slides = cached or await asyncio.to_thread(planner.plan, df, merged, row.name, limit)
    if not slides:
        _err(422, "This sheet has no chartable columns — try another sheet.")
    summary = planner.dataset_summary(df, merged)
    if not cached:
        await ds_service.save_plan(session, ds, row.name, slides, summary)
    return {"dataset_id": ds.id, "sheet": row.name, "name": ds.name,
            "summary": summary, "slides": slides}


def _apply_filters(df: pd.DataFrame, filters: list[dict]) -> pd.DataFrame:
    for f in filters or []:
        col, op, val = f.get("column"), f.get("op"), f.get("value")
        if col not in df.columns:
            continue
        try:
            if op == "in" and isinstance(val, list):
                df = df[df[col].astype(str).isin([str(v) for v in val])]
            elif op == "contains":
                df = df[df[col].astype(str).str.contains(str(val), case=False, na=False)]
            elif op in (">", ">=", "<", "<=", "==") and val is not None:
                s = pd.to_numeric(df[col], errors="coerce")
                v = float(val)
                df = df[{">": s > v, ">=": s >= v, "<": s < v, "<=": s <= v,
                         "==": s == v}[op].fillna(False)]
        except Exception:  # noqa: BLE001
            continue
    return df


@api.post("/aggregate")
async def aggregate(payload: dict = Body(...), user: User = Depends(get_current_user),
                    session: AsyncSession = Depends(get_session)):
    dsid = payload.get("dataset_id")
    spec = payload.get("spec") or {}
    if not dsid or not spec.get("type"):
        _err(400, "dataset_id and spec.type are required.")
    _ds, row, df, types = await ds_service.get_sheet(session, dsid, user.id, payload.get("sheet"))
    df = _apply_filters(df, payload.get("filters") or [])
    if df.empty:
        _err(422, "No rows match the current filters.")
    try:
        data = charting.build(df, types, dict(spec))
    except charting.ChartError as exc:
        _err(422, str(exc))
    except Exception as exc:  # noqa: BLE001
        _err(422, f"Could not build that chart: {exc}")
    meta = charting.CHART_META.get(spec["type"], (spec["type"], ""))
    return {"sheet": row.name, "type": spec["type"], "name": meta[0], "rationale": meta[1],
            "title": planner.title_for(spec, row.name), "insight": planner.insight(spec, data),
            "spec": spec, "data": data, "rows": int(len(df))}


@api.post("/describe")
async def describe(payload: dict = Body(...), user: User = Depends(get_current_user),
                   session: AsyncSession = Depends(get_session)):
    _ds, row, df, types = await ds_service.get_sheet(session, payload.get("dataset_id", ""),
                                                     user.id, payload.get("sheet"))
    num = df[[c for c, t in types.items() if t == profiling.NUM]]
    if num.empty:
        return {"sheet": row.name, "stats": []}
    desc = num.describe().T.reset_index().rename(columns={"index": "column"})
    return {"sheet": row.name,
            "stats": [{k: profiling._safe(v) for k, v in r.items()} for r in desc.to_dict("records")]}


# --------------------------------------------------------------------------- #
# NL charts
# --------------------------------------------------------------------------- #
@api.get("/nl-suggestions/{dataset_id}")
async def nl_suggestions(dataset_id: str, sheet: str | None = None,
                         user: User = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    _ds, row, df, types = await ds_service.get_sheet(session, dataset_id, user.id, sheet)
    return {"sheet": row.name, "enabled": nlchart.llm_enabled(),
            "suggestions": nlchart.suggestions(df, types)}


@api.post("/nl-chart")
async def nl_chart(payload: dict = Body(...), user: User = Depends(get_current_user),
                   session: AsyncSession = Depends(get_session)):
    dsid = payload.get("dataset_id")
    prompt = (payload.get("prompt") or "").strip()
    if not dsid or not prompt:
        _err(400, "dataset_id and prompt are required.")
    if len(prompt) > 500:
        _err(422, "Keep the request under 500 characters.")
    ds, row, df, types = await ds_service.get_sheet(session, dsid, user.id, payload.get("sheet"))
    spec, source, warning = await nlchart.propose_spec(prompt, df, types, row.name)
    try:
        slide = await asyncio.to_thread(nlchart.build_slide, dict(spec), df, types, row.name, prompt)
    except charting.ChartError as exc:
        fallback = nlchart.keyword_spec(prompt, df, types)
        if fallback != spec:
            try:
                slide = await asyncio.to_thread(nlchart.build_slide, dict(fallback), df, types,
                                                row.name, prompt)
                source, warning = "keyword", f"{exc} — used a keyword match instead."
            except charting.ChartError as exc2:
                _err(422, f"Could not build that chart: {exc2}")
        else:
            _err(422, f"Could not build that chart: {exc}")
    except Exception as exc:  # noqa: BLE001
        _err(422, f"Could not build that chart: {exc}")
    if payload.get("append", True):
        await ds_service.append_slide(session, ds, row.name, slide)
    return {"sheet": row.name, "source": source, "warning": warning,
            "llm": nlchart.llm_enabled(), "slide": slide}


# --------------------------------------------------------------------------- #
# jobs + progress (SSE)
# --------------------------------------------------------------------------- #
@api.post("/jobs")
async def create_job(payload: dict = Body(...), user: User = Depends(get_current_user),
                     session: AsyncSession = Depends(get_session)):
    dsid = payload.get("dataset_id")
    if not dsid:
        _err(400, "dataset_id is required.")
    ds = await ds_service.require_dataset(session, dsid, user.id)
    job = await job_service.create_job(session, user.id, "plan", ds.id, payload.get("sheet"))
    await session.commit()
    await job_service.enqueue(job.id, "plan")
    return {"job_id": job.id, "dataset_id": ds.id, "state": "queued"}


@api.get("/jobs/{job_id}")
async def job_status(job_id: str, user: User = Depends(get_current_user),
                     session: AsyncSession = Depends(get_session)):
    job = await job_service.get_job(session, job_id, user.id)
    if job is None:
        _err(404, "Job not found.")
    return {"job_id": job.id, "state": job.state, "pct": job.pct, "detail": job.detail,
            "error": job.error, "dataset_id": job.dataset_id,
            "result": json.loads(job.result_json or "{}")}


@api.get("/progress/{job_id}")
async def progress(job_id: str, request: Request, user: User = Depends(get_current_user),
                   session: AsyncSession = Depends(get_session)):
    """SSE stream of the real worker state for this job."""
    job = await job_service.get_job(session, job_id, user.id)
    if job is None:
        _err(404, "Job not found.")
    owner_id = user.id

    async def gen():
        def ev(state: str, pct: float, detail: str, extra: dict | None = None):
            body = {"stage": state, "pct": round(float(pct), 1), "detail": detail}
            if extra:
                body |= extra
            return f"data: {json.dumps(body, default=str)}\n\n"

        last = None
        deadline = time.time() + 300
        while time.time() < deadline:
            if await request.is_disconnected():
                return
            async with session_scope() as s:
                row = await job_service.get_job(s, job_id, owner_id)
                if row is None:
                    yield ev("error", 100, "Job disappeared.")
                    return
                state, pct, detail = row.state, row.pct, row.detail
                result = json.loads(row.result_json or "{}")
                error = row.error
            key = (state, round(pct, 1), detail)
            if key != last:
                last = key
                if state == "done":
                    yield ev("done", 100, detail, {"result": result})
                    return
                if state == "failed":
                    yield ev("error", 100, error or detail)
                    return
                yield ev(state, pct, detail)
            await asyncio.sleep(0.25)
        yield ev("error", 100, "Timed out waiting for the pipeline.")

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                                      "Connection": "keep-alive"})


# --------------------------------------------------------------------------- #
# exports (streamed to a temp file, not built fully in memory)
# --------------------------------------------------------------------------- #
async def _slides_for(session: AsyncSession, dataset_id: str, owner_id: str, sheet: str | None,
                      limit: int = 40):
    ds, row, df, types = await ds_service.get_sheet(session, dataset_id, owner_id, sheet)
    slides = await ds_service.get_plan(session, ds, row.name)
    if not slides:
        slides = await asyncio.to_thread(planner.plan, df, types, row.name, limit)
        if slides:
            await ds_service.save_plan(session, ds, row.name, slides,
                                       planner.dataset_summary(df, types))
    if not slides:
        _err(422, "This sheet has no chartable columns — try another sheet.")
    profile = json.loads(row.profile_json or "{}") or profiling.profile_sheet(row.name, df, types)
    return ds, slides, planner.dataset_summary(df, types), profile, row.name


def _spool(data: bytes, filename: str, mime: str) -> FileResponse:
    """Write the artifact to a temp file and stream it off disk."""
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=Path(filename).suffix)
    try:
        for i in range(0, len(data), 1 << 20):
            tmp.write(data[i:i + (1 << 20)])
    finally:
        tmp.close()

    def cleanup(path=tmp.name):
        try:
            os.unlink(path)
        except OSError:
            pass

    return FileResponse(tmp.name, media_type=mime, filename=filename,
                        background=BackgroundTask(cleanup),
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@api.get("/export/{dataset_id}")
async def export(dataset_id: str, sheet: str | None = None, format: str = Query("pptx"),
                 user: User = Depends(get_current_user),
                 session: AsyncSession = Depends(get_session)):
    fmt = format.lower()
    if fmt not in ("pptx", "xlsx", "pdf"):
        _err(400, "format must be pptx, xlsx or pdf.")
    ds, slides, summary, profile, key = await _slides_for(session, dataset_id, user.id, sheet)
    stem = f"{Path(ds.name).stem}-{key}".replace(" ", "-")[:48]
    title = f"{ds.name} · {key}"
    if fmt == "pptx":
        data = await asyncio.to_thread(build_pptx, title, summary, slides)
        return _spool(data, f"{stem}.pptx",
                      "application/vnd.openxmlformats-officedocument.presentationml.presentation")
    if fmt == "xlsx":
        data = await asyncio.to_thread(build_xlsx, title, summary, slides, profile)
        return _spool(data, f"{stem}.xlsx",
                      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    data = await asyncio.to_thread(build_pdf, title, summary, slides)
    return _spool(data, f"{stem}.pdf", "application/pdf")


@api.get("/export/pdf/{dataset_id}")
async def export_pdf(dataset_id: str, sheet: str | None = None,
                     user: User = Depends(get_current_user),
                     session: AsyncSession = Depends(get_session)):
    return await export(dataset_id, sheet, "pdf", user, session)


@api.get("/export/png/{dataset_id}/{slide_id}")
async def export_png(dataset_id: str, slide_id: str, sheet: str | None = None,
                     user: User = Depends(get_current_user),
                     session: AsyncSession = Depends(get_session)):
    _ds, slides, _summary, _profile, _key = await _slides_for(session, dataset_id, user.id, sheet)
    slide = next((s for s in slides if str(s.get("id")) == slide_id), None)
    if slide is None:
        _err(404, "Slide not found — regenerate the presentation.")
    data = await asyncio.to_thread(render_slide, slide)
    return _spool(data, f"{slide_id}.png", "image/png")


# --------------------------------------------------------------------------- #
# Demand Forecasting — T7-F / T7-SFG / T7-R, 7 models x 5 granularities.
# Historical rows persist in Postgres/SQLite (`ForecastRow`), scoped to the
# authenticated user — see forecast_service.py. forecasting.py itself is
# storage-agnostic pandas/numpy and untouched by that.
# --------------------------------------------------------------------------- #
def _fc_check_category(category: str | None) -> str:
    if category not in forecasting.CATEGORIES:
        _err(400, f"category must be one of {', '.join(forecasting.CATEGORIES)}")
    return category


@api.get("/forecast/meta")
async def forecast_meta():
    return {
        "categories": [{"id": k, "name": v} for k, v in forecasting.CATEGORIES.items()],
        "granularities": forecasting.GRANULARITIES,
        "models": [
            {"id": m, "name": forecasting.MODEL_NAMES[m], "best_for": forecasting.MODEL_BEST_FOR[m]}
            for m in forecasting.MODEL_ORDER
        ],
        "bundled": [{"id": k, **{kk: vv for kk, vv in v.items() if kk != "file"}}
                   for k, v in forecast_samples.BUNDLED.items()],
    }


@api.get("/forecast/data/{category}")
async def forecast_data(category: str, user: User = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    _fc_check_category(category)
    df = await forecast_service.load_df(session, user.id, category)
    rows = [] if df.empty else [
        {"date": d.date().isoformat(), "sku": s, "quantity": float(q)}
        for d, s, q in zip(df["date"], df["sku"], df["quantity"])
    ]
    skus = sorted(df["sku"].astype(str).unique().tolist()) if not df.empty else []
    return {"category": category, "rows": rows, "skus": skus, "count": len(df)}


@api.post("/forecast/data")
async def forecast_upsert(payload: dict = Body(...), user: User = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    category = _fc_check_category(payload.get("category"))
    mode = payload.get("mode", "append")
    rows = payload.get("rows") or []
    if not rows:
        _err(400, "rows is required — each row needs date, sku and quantity.")
    try:
        df = await forecast_service.upsert_rows(session, user.id, category, mode, rows)
    except (ValueError, Exception) as exc:  # noqa: BLE001
        _err(422, f"Could not parse rows — check date/sku/quantity: {exc}")
    return {"category": category, "count": len(df), "skus": sorted(df["sku"].unique().tolist())}


@api.delete("/forecast/data/{category}")
async def forecast_clear(category: str, user: User = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    _fc_check_category(category)
    await forecast_service.clear(session, user.id, category)
    return {"category": category, "count": 0, "skus": []}


@api.post("/forecast/sample/{category}")
async def forecast_load_sample(category: str, user: User = Depends(get_current_user),
                               session: AsyncSession = Depends(get_session)):
    _fc_check_category(category)
    df = await forecast_service.load_sample(session, user.id, category)
    return {"category": category, "count": len(df), "skus": sorted(df["sku"].unique().tolist())}


@api.post("/forecast/bundled/{name}")
async def forecast_load_bundled(name: str, payload: dict = Body(...),
                                user: User = Depends(get_current_user),
                                session: AsyncSession = Depends(get_session)):
    category = _fc_check_category(payload.get("category"))
    if name not in forecast_samples.BUNDLED:
        _err(404, f"Unknown bundled dataset '{name}'.")
    df = await forecast_service.load_bundled(session, user.id, category, name)
    return {"category": category, "count": len(df), "skus": sorted(df["sku"].unique().tolist())}


@api.post("/forecast/run")
async def forecast_run(payload: dict = Body(...), user: User = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    category = _fc_check_category(payload.get("category"))
    granularity = payload.get("granularity", "monthly")
    if granularity not in forecasting.GRANULARITIES:
        _err(400, f"granularity must be one of {', '.join(forecasting.GRANULARITIES)}")
    sku = payload.get("sku") or "__all__"

    df = await forecast_service.load_df(session, user.id, category)
    if df.empty:
        _err(422, "No historical demand data for this category yet — add rows or load sample data.")
    skus = sorted(df["sku"].unique().tolist())
    if sku != "__all__" and sku not in skus:
        _err(404, f"Unknown SKU '{sku}' for {category}.")

    series = forecasting.aggregate(df, granularity, sku)
    if series.empty or len(series) < 2:
        _err(422, "Not enough history at this granularity to forecast — add more periods of data.")

    results, pattern, recommended = forecasting.run_all_models(series, granularity)
    idx = series.index
    h = forecasting.HORIZON[granularity]
    future_idx = pd.date_range(idx[-1], periods=h + 1, freq=forecasting._FREQ[granularity])[1:]
    actual_labels = [forecasting.label_for(t, granularity) for t in idx]
    future_labels = [forecasting.label_for(t, granularity) for t in future_idx]
    tail = min(12, len(idx))

    models_out = []
    for r in results:
        fc_vals = [round(float(v), 2) for v in r.forecast.tolist()]
        line = {
            "labels": actual_labels + future_labels,
            "datasets": [
                {"label": "Actual", "data": [round(float(v), 2) for v in series.values.tolist()] + [None] * h},
                {"label": "Fitted", "data": [round(float(v), 2) for v in r.fitted.tolist()] + [None] * h},
                {"label": "Forecast", "data": [None] * len(idx) + fc_vals},
            ],
        }
        bar = {
            "labels": actual_labels[-tail:] + future_labels,
            "datasets": [
                {"label": "Actual",
                 "data": [round(float(v), 2) for v in series.values.tolist()[-tail:]] + [None] * h},
                {"label": "Forecast", "data": [None] * tail + fc_vals},
            ],
        }
        total_fc = sum(fc_vals) or 1.0
        pie = {
            "labels": future_labels,
            "datasets": [{"data": [round(100 * v / total_fc, 1) for v in fc_vals]}],
        }
        models_out.append({
            "model": r.model, "name": r.name, "best_for": r.best_for,
            "is_recommended": r.is_recommended, "metrics": r.metrics, "meta": r.meta,
            "chart_line": line, "chart_bar": bar, "chart_pie": pie,
            # Raw future values per model, used by the Future Demand table —
            # all 7 models' forecasts side by side, not just the recommended one.
            "future": fc_vals,
        })

    ribbon3d = {
        "z": [[round(float(v), 2) for v in r.forecast.tolist()] for r in results],
        "axes": ["Time", "Model", "Demand"],
        "xTicks": future_labels,
        "yTicks": [r.name for r in results],
    }

    out = {
        "category": category, "granularity": granularity, "sku": sku, "skus": skus,
        "pattern": pattern, "recommended": recommended,
        "actual": {"labels": actual_labels, "values": [round(float(v), 2) for v in series.values.tolist()]},
        "future_labels": future_labels,
        "models": models_out,
        "ribbon3d": ribbon3d,
    }

    if sku == "__all__" and len(skus) >= 2:
        top_skus = skus[:10]
        combo_labels = actual_labels + future_labels
        z = []
        for s in top_skus:
            ss = forecasting.aggregate(df, granularity, s)
            if ss.empty:
                z.append([0.0] * len(combo_labels))
                continue
            res_s, _pattern_s, rec_s = forecasting.run_all_models(ss, granularity)
            pick = next((m for m in res_s if m.model == rec_s), res_s[0])
            hist_vals = ss.reindex(idx, fill_value=0.0).tolist()
            row = [round(float(v), 2) for v in hist_vals] + [round(float(v), 2) for v in pick.forecast.tolist()]
            z.append(row)
        out["surface3d"] = {"z": z, "axes": ["Time", "SKU", "Demand"], "xTicks": combo_labels, "yTicks": top_skus}

    return out


app.include_router(api)


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)),
                log_level=settings.log_level.lower())
