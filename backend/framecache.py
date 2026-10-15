"""Parsed-frame cache.

Frames are persisted as parquet in object storage, keyed by dataset + sheet.
Nothing is kept in a module-level dict: every request loads from parquet.
A TTL sweeper drops cold parquet files; they are transparently rebuilt from the
raw upload that still lives in object storage.
"""
from __future__ import annotations

import asyncio
import io
import re
import time

import pandas as pd

from config import settings
from storage import Storage, StorageError, get_storage

PREFIX = "frames"
RAW_PREFIX = "raw"


def slug(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", name)[:80] or "sheet"


def frame_key(dataset_id: str, sheet: str) -> str:
    return f"{PREFIX}/{dataset_id}/{slug(sheet)}.parquet"


def raw_key(dataset_id: str, filename: str) -> str:
    return f"{RAW_PREFIX}/{dataset_id}/{slug(filename)}"


def _dedupe(df: pd.DataFrame) -> pd.DataFrame:
    if df.columns.duplicated().any():
        cols, seen = [], {}
        for c in df.columns:
            n = seen.get(c, 0)
            seen[c] = n + 1
            cols.append(c if n == 0 else f"{c}_{n}")
        df = df.copy()
        df.columns = cols
    return df


def serialize(df: pd.DataFrame) -> tuple[bytes, str]:
    """Parquet bytes; falls back to pickle for frames pyarrow refuses."""
    df = _dedupe(df)
    buf = io.BytesIO()
    try:
        df.to_parquet(buf, engine="pyarrow", index=False)
        return buf.getvalue(), "parquet"
    except Exception:
        safe = df.copy()
        for c in safe.columns:
            if safe[c].dtype == object:
                safe[c] = safe[c].astype(str)
        buf = io.BytesIO()
        try:
            safe.to_parquet(buf, engine="pyarrow", index=False)
            return buf.getvalue(), "parquet"
        except Exception:
            buf = io.BytesIO()
            df.to_pickle(buf)
            return buf.getvalue(), "pickle"


def deserialize(data: bytes) -> pd.DataFrame:
    try:
        return pd.read_parquet(io.BytesIO(data), engine="pyarrow")
    except Exception:
        return pd.read_pickle(io.BytesIO(data))


def save_frame(dataset_id: str, sheet: str, df: pd.DataFrame,
               store: Storage | None = None) -> str:
    store = store or get_storage()
    key = frame_key(dataset_id, sheet)
    data, _fmt = serialize(df)
    store.put(key, data)
    return key


def load_frame(dataset_id: str, sheet: str, store: Storage | None = None) -> pd.DataFrame | None:
    store = store or get_storage()
    key = frame_key(dataset_id, sheet)
    try:
        return deserialize(store.get(key))
    except StorageError:
        return None
    except Exception:
        return None


def sweep(ttl_minutes: int | None = None, store: Storage | None = None) -> int:
    """Delete parquet frames untouched for longer than the TTL. Returns count."""
    store = store or get_storage()
    ttl = (ttl_minutes if ttl_minutes is not None else settings.parquet_ttl_min) * 60
    if ttl <= 0:
        return 0
    cutoff = time.time() - ttl
    removed = 0
    for key in store.list(PREFIX):
        try:
            if store.mtime(key) and store.mtime(key) < cutoff:
                store.delete(key)
                removed += 1
        except Exception:
            continue
    return removed


async def sweeper_loop(interval_s: int = 600) -> None:
    """Background TTL sweeper. Cancelled on shutdown."""
    while True:
        try:
            await asyncio.sleep(interval_s)
            await asyncio.to_thread(sweep)
        except asyncio.CancelledError:
            raise
        except Exception:
            continue
