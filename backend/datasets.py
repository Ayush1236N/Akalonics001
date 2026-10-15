"""Dataset service layer: ingest, ownership-scoped reads, plan persistence."""
from __future__ import annotations

import json

import pandas as pd
from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

import framecache
import planner
import profiling
import uploads
from models import ColumnMeta, Dataset, Presentation, Sheet, Slide
from profiling import ParseError
from storage import get_storage

NOT_FOUND = "That dataset is not available — upload the file again."


# --------------------------------------------------------------------------- #
# ingest
# --------------------------------------------------------------------------- #
def parse_bytes(filename: str, raw: bytes) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """Sniff, guard, parse and sanitize. Raises ParseError/UnsafeUpload."""
    ext = uploads.sniff(filename, raw)
    stem = filename.rsplit(".", 1)[0] if "." in filename else (filename or "upload")
    frames = profiling.read_any(f"{stem}{ext}", raw)
    return uploads.sanitize(frames)


async def create_dataset(session: AsyncSession, owner_id: str, name: str, filename: str,
                         raw: bytes, status: str = "pending") -> Dataset:
    ds = Dataset(owner_id=owner_id, name=name, original_filename=filename,
                 size_bytes=len(raw), status=status)
    session.add(ds)
    await session.flush()
    key = framecache.raw_key(ds.id, filename or "upload.bin")
    get_storage().put(key, raw)
    ds.storage_key = key
    await session.flush()
    return ds


async def ingest_frames(session: AsyncSession, ds: Dataset,
                        frames: dict[str, pd.DataFrame]) -> Dataset:
    """Coerce types, persist parquet frames + sheet/column metadata."""
    await session.execute(delete(Sheet).where(Sheet.dataset_id == ds.id))
    kept = 0
    for i, (sheet, rawdf) in enumerate(frames.items()):
        df, types = profiling.coerce(rawdf)
        if df.empty or not len(df.columns):
            continue
        prof = profiling.profile_sheet(sheet, df, types)
        key = framecache.save_frame(ds.id, sheet, df)
        row = Sheet(dataset_id=ds.id, name=sheet, position=i, n_rows=int(len(df)),
                    n_cols=int(len(df.columns)), parquet_key=key,
                    profile_json=json.dumps(prof, default=str),
                    types_json=json.dumps(types))
        session.add(row)
        await session.flush()
        for col in prof.get("columns", []):
            session.add(ColumnMeta(
                sheet_id=row.id, name=str(col.get("name")), dtype=str(col.get("type")),
                cardinality=int(col.get("unique") or 0),
                null_frac=(float(col.get("nulls") or 0) / max(1, int(prof.get("rows") or 1))),
                samples_json=json.dumps(col.get("sample") or [], default=str)))
        kept += 1
    if not kept:
        ds.status = "failed"
        ds.error = "The file parsed but contained no usable rows."
        raise ParseError(ds.error)
    ds.status = "ready"
    ds.error = None
    await session.flush()
    return ds


async def ingest_upload(session: AsyncSession, owner_id: str, filename: str,
                        raw: bytes) -> Dataset:
    frames, _notes = parse_bytes(filename, raw)
    ds = await create_dataset(session, owner_id, filename or "Uploaded data", filename, raw)
    return await ingest_frames(session, ds, frames)


# --------------------------------------------------------------------------- #
# reads (always ownership scoped -> 404, never 403)
# --------------------------------------------------------------------------- #
async def require_dataset(session: AsyncSession, dataset_id: str, owner_id: str) -> Dataset:
    ds = (await session.execute(
        select(Dataset).where(Dataset.id == dataset_id, Dataset.owner_id == owner_id)
    )).scalar_one_or_none()
    if ds is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    return ds


async def sheets_of(session: AsyncSession, ds: Dataset) -> list[Sheet]:
    return list((await session.execute(
        select(Sheet).where(Sheet.dataset_id == ds.id).order_by(Sheet.position)
    )).scalars().all())


async def dataset_payload(session: AsyncSession, ds: Dataset) -> dict:
    rows = await sheets_of(session, ds)
    profiles = [json.loads(s.profile_json or "{}") for s in rows]
    summary = {}
    for s in rows:
        df = await load_frame(session, ds, s)
        types = json.loads(s.types_json or "{}")
        summary[s.name] = planner.dataset_summary(df, types)
    return {"dataset_id": ds.id, "name": ds.name, "status": ds.status,
            "sheets": profiles, "summary": summary}


async def load_frame(session: AsyncSession, ds: Dataset, sheet: Sheet) -> pd.DataFrame:
    """Load parquet; rebuild from the raw upload when the TTL sweeper removed it."""
    df = framecache.load_frame(ds.id, sheet.name)
    if df is not None:
        return df
    if not ds.storage_key:
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    try:
        raw = get_storage().get(ds.storage_key)
        frames, _ = parse_bytes(ds.original_filename or ds.name, raw)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=404, detail=NOT_FOUND) from exc
    target = frames.get(sheet.name)
    if target is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    df, _types = profiling.coerce(target)
    framecache.save_frame(ds.id, sheet.name, df)
    return df


async def get_sheet(session: AsyncSession, dataset_id: str, owner_id: str,
                    sheet: str | None) -> tuple[Dataset, Sheet, pd.DataFrame, dict[str, str]]:
    ds = await require_dataset(session, dataset_id, owner_id)
    rows = await sheets_of(session, ds)
    if not rows:
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    row = next((s for s in rows if s.name == sheet), rows[0])
    df = await load_frame(session, ds, row)
    types = json.loads(row.types_json or "{}")
    types = {k: v for k, v in types.items() if k in df.columns}
    return ds, row, df, types


# --------------------------------------------------------------------------- #
# presentations / slides
# --------------------------------------------------------------------------- #
async def save_plan(session: AsyncSession, ds: Dataset, sheet: str, slides: list[dict],
                    summary: dict, title: str = "") -> Presentation:
    existing = (await session.execute(
        select(Presentation).where(Presentation.dataset_id == ds.id, Presentation.sheet == sheet)
    )).scalars().all()
    for p in existing:
        await session.delete(p)
    await session.flush()
    pres = Presentation(owner_id=ds.owner_id, dataset_id=ds.id, sheet=sheet,
                        title=title or f"{ds.name} · {sheet}",
                        summary_json=json.dumps(summary, default=str))
    session.add(pres)
    await session.flush()
    for i, s in enumerate(slides):
        session.add(Slide(presentation_id=pres.id, position=i, slide_key=str(s.get("id") or i),
                          chart_type=str(s.get("type") or ""), title=str(s.get("title") or ""),
                          payload_json=json.dumps(s, default=str)))
    await session.flush()
    return pres


async def get_plan(session: AsyncSession, ds: Dataset, sheet: str) -> list[dict]:
    pres = (await session.execute(
        select(Presentation).where(Presentation.dataset_id == ds.id, Presentation.sheet == sheet)
        .order_by(Presentation.created_at.desc())
    )).scalars().first()
    if pres is None:
        return []
    rows = (await session.execute(
        select(Slide).where(Slide.presentation_id == pres.id).order_by(Slide.position)
    )).scalars().all()
    return [json.loads(s.payload_json) for s in rows]


async def append_slide(session: AsyncSession, ds: Dataset, sheet: str, slide: dict,
                       summary: dict | None = None) -> None:
    pres = (await session.execute(
        select(Presentation).where(Presentation.dataset_id == ds.id, Presentation.sheet == sheet)
        .order_by(Presentation.created_at.desc())
    )).scalars().first()
    if pres is None:
        pres = Presentation(owner_id=ds.owner_id, dataset_id=ds.id, sheet=sheet,
                            title=f"{ds.name} · {sheet}",
                            summary_json=json.dumps(summary or {}, default=str))
        session.add(pres)
        await session.flush()
    n = len((await session.execute(
        select(Slide).where(Slide.presentation_id == pres.id))).scalars().all())
    session.add(Slide(presentation_id=pres.id, position=n, slide_key=str(slide.get("id") or n),
                      chart_type=str(slide.get("type") or ""), title=str(slide.get("title") or ""),
                      payload_json=json.dumps(slide, default=str)))
    await session.flush()
