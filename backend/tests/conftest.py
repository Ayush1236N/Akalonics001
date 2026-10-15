"""Test harness: isolated SQLite + local storage per session, real HTTP via httpx."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

_TMP = tempfile.mkdtemp(prefix="akalonics-tests-")
os.environ.update(
    DATA_DIR=_TMP,
    STORAGE_DIR=str(Path(_TMP) / "objects"),
    STORAGE_BACKEND="local",
    DATABASE_URL="",
    REDIS_URL="",
    JWT_SECRET="test-secret-test-secret-test-secret-32",
    DEBUG="true",
    COOKIE_SAMESITE="lax",
    COOKIE_SECURE="false",
    RATE_LIMIT_ENABLED="true",
    RATE_UPLOADS="4/minute",
    RATE_API="5000/minute",
    RATE_AUTH="2000/minute",
    NL_CHARTS_ENABLED="true",
    USE_WORKER="false",
    MAX_UPLOAD_MB="25",
)
os.environ.pop("ANTHROPIC_API_KEY", None)  # LLM off by default -> keyword matcher

import httpx  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

SAMPLES = BACKEND.parent / "samples"


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    shutil.rmtree(_TMP, ignore_errors=True)


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest_asyncio.fixture
async def client():
    from app import app
    from db import init_models

    await init_models()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                 timeout=120) as c:
        yield c


async def new_guest(client: httpx.AsyncClient) -> dict:
    r = await client.post("/api/auth/guest")
    assert r.status_code == 200, r.text
    body = r.json()
    return {"headers": {"Authorization": f"Bearer {body['access_token']}"}, **body}


@pytest_asyncio.fixture
async def guest(client):
    return await new_guest(client)


@pytest_asyncio.fixture
async def dataset(client, guest):
    r = await client.post("/api/sample/sales.xlsx", headers=guest["headers"])
    assert r.status_code == 200, r.text
    return r.json()


def sample_bytes(name: str) -> bytes:
    return (SAMPLES / name).read_bytes()
