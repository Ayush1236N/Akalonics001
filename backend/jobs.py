"""Async work: job records in the DB, executed either by an arq worker (Redis)
or by an in-process asyncio task when no Redis is configured.

States: queued -> parsing -> profiling -> planning -> done | failed (dead-letter).
"""
from __future__ import annotations

import asyncio
import json
import logging

from sqlalchemy import select

import datasets as ds_service
import planner
from config import settings
from db import session_scope
from models import Dataset, Job, Sheet
from profiling import ParseError
from storage import get_storage

log = logging.getLogger("akalonics.jobs")

STATES = ("queued", "parsing", "profiling", "planning", "done", "failed")
MAX_ATTEMPTS = 2
_tasks: set[asyncio.Task] = set()


async def create_job(session, owner_id: str, kind: str, dataset_id: str | None = None,
                     sheet: str | None = None) -> Job:
    job = Job(owner_id=owner_id, kind=kind, dataset_id=dataset_id, sheet=sheet,
              state="queued", pct=2.0, detail="Queued")
    session.add(job)
    await session.flush()
    return job


async def _set(job_id: str, **fields) -> None:
    async with session_scope() as s:
        job = (await s.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
        if job is None:
            return
        for k, v in fields.items():
            setattr(job, k, v)


async def get_job(session, job_id: str, owner_id: str | None = None) -> Job | None:
    q = select(Job).where(Job.id == job_id)
    if owner_id:
        q = q.where(Job.owner_id == owner_id)
    return (await session.execute(q)).scalar_one_or_none()


# --------------------------------------------------------------------------- #
# the actual work
# --------------------------------------------------------------------------- #
async def _run_ingest(job_id: str) -> dict:
    async with session_scope() as s:
        job = (await s.execute(select(Job).where(Job.id == job_id))).scalar_one()
        dataset_id, sheet_hint = job.dataset_id, job.sheet
        ds = (await s.execute(select(Dataset).where(Dataset.id == dataset_id))).scalar_one()
        raw_key, filename, name = ds.storage_key, ds.original_filename, ds.name

    await _set(job_id, state="parsing", pct=12.0, detail=f"Parsing {name}")
    raw = await asyncio.to_thread(get_storage().get, raw_key)
    frames, notes = await asyncio.to_thread(ds_service.parse_bytes, filename or name, raw)

    await _set(job_id, state="profiling", pct=38.0,
               detail=f"Profiling {len(frames)} sheet(s)")
    async with session_scope() as s:
        ds = (await s.execute(select(Dataset).where(Dataset.id == dataset_id))).scalar_one()
        await ds_service.ingest_frames(s, ds, frames)
        payload = await ds_service.dataset_payload(s, ds)

    await _set(job_id, state="planning", pct=68.0, detail="Scoring chart candidates")
    async with session_scope() as s:
        ds = (await s.execute(select(Dataset).where(Dataset.id == dataset_id))).scalar_one()
        rows = await ds_service.sheets_of(s, ds)
        first = next((r for r in rows if r.name == sheet_hint), rows[0])
        df = await ds_service.load_frame(s, ds, first)
        types = json.loads(first.types_json or "{}")
        slides = await asyncio.to_thread(planner.plan, df, types, first.name, 40)
        if slides:
            await ds_service.save_plan(s, ds, first.name, slides,
                                       planner.dataset_summary(df, types))
        count = len(slides)

    result = {"dataset_id": dataset_id, "sheet": payload["sheets"][0]["name"] if payload["sheets"] else None,
              "slides": count, "notes": notes, "dataset": payload}
    await _set(job_id, state="done", pct=100.0, detail=f"{count} charts ready",
               result_json=json.dumps(result, default=str))
    return result


async def _run_plan(job_id: str) -> dict:
    async with session_scope() as s:
        job = (await s.execute(select(Job).where(Job.id == job_id))).scalar_one()
        dataset_id, sheet_name, owner_id = job.dataset_id, job.sheet, job.owner_id

    await _set(job_id, state="parsing", pct=10.0, detail="Reading cached frame")
    async with session_scope() as s:
        ds, sheet, df, types = await ds_service.get_sheet(s, dataset_id, owner_id, sheet_name)
        rows, cols = len(df), len(df.columns)
        name = sheet.name
    await _set(job_id, state="profiling", pct=30.0, detail=f"{rows:,} rows · {cols} columns")
    await _set(job_id, state="planning", pct=55.0, detail="Scoring chart candidates")
    async with session_scope() as s:
        ds, sheet, df, types = await ds_service.get_sheet(s, dataset_id, owner_id, name)
        slides = await asyncio.to_thread(planner.plan, df, types, name, 40)
        if not slides:
            raise ParseError("This sheet has no chartable columns — try another sheet.")
        await ds_service.save_plan(s, ds, name, slides, planner.dataset_summary(df, types))
    result = {"dataset_id": dataset_id, "sheet": name, "slides": len(slides)}
    await _set(job_id, state="done", pct=100.0, detail=f"{len(slides)} charts ready",
               result_json=json.dumps(result, default=str))
    return result


RUNNERS = {"ingest": _run_ingest, "plan": _run_plan}


async def run_job(job_id: str, kind: str | None = None) -> dict | None:
    """Execute with retry + backoff; dead-letters the job on repeated failure."""
    if kind is None:
        async with session_scope() as s:
            job = (await s.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
            kind = job.kind if job else "ingest"
    runner = RUNNERS.get(kind or "ingest", _run_ingest)
    last: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            await _set(job_id, attempts=attempt)
            return await runner(job_id)
        except (ParseError,) as exc:
            await _set(job_id, state="failed", pct=100.0, detail=str(exc), error=str(exc))
            return None
        except Exception as exc:  # noqa: BLE001
            last = exc
            log.warning("job %s attempt %s failed: %s", job_id, attempt, exc)
            if attempt < MAX_ATTEMPTS:
                await asyncio.sleep(0.5 * 2 ** (attempt - 1))
    msg = f"The pipeline failed: {last}"
    await _set(job_id, state="failed", pct=100.0, detail=msg, error=str(last))
    async with session_scope() as s:
        job = (await s.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
        if job and job.dataset_id:
            d = (await s.execute(select(Dataset).where(Dataset.id == job.dataset_id))).scalar_one_or_none()
            if d is not None:
                d.status, d.error = "failed", msg
    return None


# --------------------------------------------------------------------------- #
# enqueue
# --------------------------------------------------------------------------- #
def worker_enabled() -> bool:
    return bool(settings.redis_url and settings.use_worker)


async def enqueue(job_id: str, kind: str) -> str:
    """Redis/arq when configured, otherwise an in-process task (zero-infra dev)."""
    if worker_enabled():
        try:
            from arq import create_pool
            from arq.connections import RedisSettings
            pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
            await pool.enqueue_job("run_job", job_id, kind)
            await pool.close()
            return "arq"
        except Exception as exc:  # noqa: BLE001
            log.warning("arq enqueue failed (%s) — running in-process", exc)
    task = asyncio.create_task(run_job(job_id, kind))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return "inprocess"


async def drain() -> None:
    """Graceful shutdown: wait for in-process jobs."""
    if _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


async def sheet_names(session, dataset_id: str) -> list[str]:
    return [r.name for r in (await session.execute(
        select(Sheet).where(Sheet.dataset_id == dataset_id).order_by(Sheet.position)
    )).scalars().all()]
