"""Config, storage backends, parquet cache, rate limits, health/ready."""
from __future__ import annotations

import io

import pandas as pd
import pytest

import framecache
from config import Settings
from storage import LocalStorage, StorageError

pytestmark = pytest.mark.asyncio


# --- config -----------------------------------------------------------------
def test_sqlite_fallback_when_no_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    s = Settings(DATABASE_URL=None)
    assert s.sqlalchemy_url.startswith("sqlite+aiosqlite:///")
    assert s.is_sqlite is True


def test_postgres_url_is_normalised():
    s = Settings(DATABASE_URL="postgres://u:p@db:5432/x")
    assert s.sqlalchemy_url == "postgresql+asyncpg://u:p@db:5432/x"
    assert s.sync_sqlalchemy_url == "postgresql+psycopg2://u:p@db:5432/x"
    assert s.is_sqlite is False


def test_origins_parsing():
    s = Settings(ALLOWED_ORIGINS="http://a.com, http://b.com ,")
    assert s.origins == ["http://a.com", "http://b.com"]


def test_llm_flag_off_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert Settings(NL_CHARTS_ENABLED=True).llm_available is False
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert Settings(NL_CHARTS_ENABLED=True).llm_available is True
    assert Settings(NL_CHARTS_ENABLED=False).llm_available is False


# --- storage ----------------------------------------------------------------
def test_local_storage_roundtrip(tmp_path):
    st = LocalStorage(tmp_path)
    st.put("a/b.txt", b"hello")
    assert st.get("a/b.txt") == b"hello"
    assert st.exists("a/b.txt") and "a/b.txt" in st.list("a/")
    assert st.mtime("a/b.txt") > 0
    st.delete("a/b.txt")
    assert not st.exists("a/b.txt")
    with pytest.raises(StorageError):
        st.get("a/b.txt")
    assert st.healthy() is True


def test_local_storage_rejects_traversal(tmp_path):
    st = LocalStorage(tmp_path)
    with pytest.raises(StorageError):
        st.put("../escape.txt", b"nope")


def test_s3_storage_requires_bucket(monkeypatch):
    from storage import S3Storage
    from config import settings
    monkeypatch.setattr(settings, "s3_bucket", None)
    with pytest.raises(StorageError):
        S3Storage()


# --- parquet frame cache ----------------------------------------------------
def test_parquet_roundtrip_preserves_dtypes(tmp_path):
    st = LocalStorage(tmp_path)
    df = pd.DataFrame({"d": pd.date_range("2024-01-01", periods=3),
                       "n": [1.5, 2.5, 3.5], "c": ["a", "b", "c"]})
    framecache.save_frame("ds1", "Sheet 1", df, store=st)
    out = framecache.load_frame("ds1", "Sheet 1", store=st)
    assert list(out.columns) == ["d", "n", "c"]
    assert out["n"].tolist() == [1.5, 2.5, 3.5]
    assert str(out["d"].dtype).startswith("datetime64")


def test_parquet_handles_duplicate_and_mixed_columns(tmp_path):
    st = LocalStorage(tmp_path)
    df = pd.DataFrame([[1, "a", {"x": 1}], [2, "b", {"y": 2}]], columns=["n", "n", "obj"])
    framecache.save_frame("ds2", "S", df, store=st)
    out = framecache.load_frame("ds2", "S", store=st)
    assert len(out) == 2 and len(out.columns) == 3


def test_missing_frame_returns_none(tmp_path):
    assert framecache.load_frame("nope", "S", store=LocalStorage(tmp_path)) is None


def test_ttl_sweeper_removes_cold_frames(tmp_path):
    st = LocalStorage(tmp_path)
    framecache.save_frame("ds3", "S", pd.DataFrame({"a": [1]}), store=st)
    assert framecache.sweep(ttl_minutes=10_000, store=st) == 0
    assert framecache.sweep(ttl_minutes=0, store=st) == 0  # 0 disables
    assert framecache.sweep(ttl_minutes=-1, store=st) == 0
    import os
    key = framecache.frame_key("ds3", "S")
    path = tmp_path / key
    os.utime(path, (100, 100))
    assert framecache.sweep(ttl_minutes=1, store=st) == 1
    assert framecache.load_frame("ds3", "S", store=st) is None


async def test_frame_is_rebuilt_after_sweep(client, guest, dataset):
    """The parquet cache is disposable: reads rebuild it from the raw upload."""
    from storage import get_storage
    store = get_storage()
    for key in store.list(framecache.PREFIX + f"/{dataset['dataset_id']}"):
        store.delete(key)
    r = await client.get(f"/api/rows/{dataset['dataset_id']}", headers=guest["headers"])
    assert r.status_code == 200 and r.json()["total"] > 0


# --- health / ready ---------------------------------------------------------
async def test_health_and_ready(client):
    h = await client.get("/api/health")
    assert h.status_code == 200 and h.json()["charts"] == 37
    r = await client.get("/api/ready")
    assert r.status_code == 200
    assert r.json()["checks"] == {"database": True, "storage": True, "redis": True}


async def test_config_exposes_feature_flags(client):
    c = (await client.get("/api/config")).json()
    assert c["nl_charts"] is False and c["guest_mode"] is True and c["chart_types"] == 37


async def test_request_id_header(client):
    r = await client.get("/api/health", headers={"x-request-id": "abc123"})
    assert r.headers["x-request-id"] == "abc123"


# --- rate limiting ----------------------------------------------------------
async def test_upload_rate_limit_returns_429(client, guest):
    """RATE_UPLOADS is 4/minute in the test env."""
    codes = []
    for i in range(6):
        r = await client.post("/api/upload",
                              files={"file": (f"t{i}.csv", io.BytesIO(b"a,b\n1,2\n3,4\n"),
                                              "text/csv")},
                              headers=guest["headers"])
        codes.append(r.status_code)
    assert 429 in codes, codes
    assert codes.count(200) <= 4
    body = [c for c in codes if c == 429]
    assert body


async def test_rate_limit_is_per_user(client):
    from conftest import new_guest
    a, b = await new_guest(client), await new_guest(client)
    for i in range(5):
        await client.post("/api/upload",
                          files={"file": (f"a{i}.csv", io.BytesIO(b"a,b\n1,2\n"), "text/csv")},
                          headers=a["headers"])
    r = await client.post("/api/upload",
                          files={"file": ("b.csv", io.BytesIO(b"a,b\n1,2\n3,4\n"), "text/csv")},
                          headers=b["headers"])
    assert r.status_code == 200, r.text
