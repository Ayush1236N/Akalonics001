"""arq worker entrypoint:  arq worker.WorkerSettings

Runs the same job functions the API falls back to in-process, so behaviour is
identical with and without Redis.
"""
from __future__ import annotations

import asyncio
import logging

from arq.connections import RedisSettings

import jobs
from config import settings
from db import init_models, reset_engine
from logging_setup import configure_logging

log = logging.getLogger("akalonics.worker")


async def run_job(ctx, job_id: str, kind: str = "ingest"):
    return await jobs.run_job(job_id, kind)


async def startup(ctx):
    configure_logging()
    if settings.is_sqlite:
        await init_models()
    log.info("worker started", extra={"database": settings.sqlalchemy_url.split("@")[-1]})


async def shutdown(ctx):
    await jobs.drain()
    await reset_engine()
    log.info("worker stopped")


class WorkerSettings:
    functions = [run_job]
    on_startup = startup
    on_shutdown = shutdown
    max_tries = 3
    job_timeout = 600
    keep_result = 3600
    redis_settings = RedisSettings.from_dsn(settings.redis_url or "redis://localhost:6379")


if __name__ == "__main__":
    # Allows `python worker.py <job_id> <kind>` for local debugging without Redis.
    import sys

    if len(sys.argv) > 1:
        asyncio.run(jobs.run_job(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "ingest"))
    else:
        from arq import run_worker

        run_worker(WorkerSettings)  # type: ignore[arg-type]
