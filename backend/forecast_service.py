"""Demand Forecasting data-access layer.

Replaces the pickle/CSV cache the feature originally shipped with: historical
demand rows now live in Postgres/SQLite (`ForecastRow`), scoped per user and
category, exactly like every other resource in the app. `forecasting.py`
itself is untouched — it only ever sees a plain pandas DataFrame with
{date, sku, quantity} columns, so the seven models and the routing logic
don't know or care where the rows came from.
"""
from __future__ import annotations

import pandas as pd
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

import forecast_samples
from models import ForecastRow

CATEGORIES = ("T7-F", "T7-SFG", "T7-R")


async def load_df(session: AsyncSession, owner_id: str, category: str) -> pd.DataFrame:
    rows = (await session.execute(
        select(ForecastRow.date, ForecastRow.sku, ForecastRow.quantity)
        .where(ForecastRow.owner_id == owner_id, ForecastRow.category == category)
        .order_by(ForecastRow.date)
    )).all()
    if not rows:
        df = pd.DataFrame(columns=["date", "sku", "quantity"])
        df["date"] = pd.to_datetime(df["date"])
        return df
    df = pd.DataFrame(rows, columns=["date", "sku", "quantity"])
    df["date"] = pd.to_datetime(df["date"])
    df["quantity"] = df["quantity"].astype(float)
    return df


async def _replace(session: AsyncSession, owner_id: str, category: str, df: pd.DataFrame) -> None:
    await session.execute(
        delete(ForecastRow).where(ForecastRow.owner_id == owner_id, ForecastRow.category == category)
    )
    for rec in df.to_dict("records"):
        session.add(ForecastRow(owner_id=owner_id, category=category,
                                date=pd.Timestamp(rec["date"]).to_pydatetime(),
                                sku=str(rec["sku"]), quantity=float(rec["quantity"])))
    await session.flush()


async def upsert_rows(session: AsyncSession, owner_id: str, category: str, mode: str,
                      rows: list[dict]) -> pd.DataFrame:
    new = pd.DataFrame(rows)
    new["date"] = pd.to_datetime(new["date"])
    new["quantity"] = pd.to_numeric(new["quantity"])
    new["sku"] = new["sku"].astype(str).str.strip()
    new = new[["date", "sku", "quantity"]]
    if new["quantity"].isna().any() or (new["quantity"] < 0).any():
        raise ValueError("Quantity must be a non-negative number on every row.")
    if new["sku"].eq("").any():
        raise ValueError("Every row needs a SKU / item identifier.")

    if mode == "replace":
        await _replace(session, owner_id, category, new)
        return new.sort_values("date").reset_index(drop=True)

    for rec in new.to_dict("records"):
        session.add(ForecastRow(owner_id=owner_id, category=category,
                                date=pd.Timestamp(rec["date"]).to_pydatetime(),
                                sku=str(rec["sku"]), quantity=float(rec["quantity"])))
    await session.flush()
    return await load_df(session, owner_id, category)


async def clear(session: AsyncSession, owner_id: str, category: str) -> None:
    await session.execute(
        delete(ForecastRow).where(ForecastRow.owner_id == owner_id, ForecastRow.category == category)
    )


async def load_sample(session: AsyncSession, owner_id: str, category: str) -> pd.DataFrame:
    """Synthetic demo history — replaces whatever is currently loaded for this category."""
    df = forecast_samples.generate(category)
    await _replace(session, owner_id, category, df)
    return df


async def load_bundled(session: AsyncSession, owner_id: str, category: str, name: str) -> pd.DataFrame:
    """A real, bundled dataset (e.g. the user's own uploaded history) — also replaces."""
    df = forecast_samples.load_bundled(name)
    await _replace(session, owner_id, category, df)
    return df
