"""SQLAlchemy 2.x ORM models."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (Boolean, DateTime, Float, ForeignKey, Integer, String,
                        Text, UniqueConstraint)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def uid() -> str:
    return uuid.uuid4().hex[:12]


def now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    email: Mapped[str | None] = mapped_column(String(320), unique=True, nullable=True, index=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_guest: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    datasets: Mapped[list["Dataset"]] = relationship(back_populates="owner")


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id", ondelete="CASCADE"),
                                         index=True)
    token_hash: Mapped[str] = mapped_column(String(128), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id", ondelete="CASCADE"),
                                         index=True)
    name: Mapped[str] = mapped_column(String(120), default="default")
    key_hash: Mapped[str] = mapped_column(String(128), index=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Dataset(Base):
    __tablename__ = "datasets"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id", ondelete="CASCADE"),
                                          index=True)
    name: Mapped[str] = mapped_column(String(255))
    original_filename: Mapped[str] = mapped_column(String(255), default="")
    storage_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="ready")  # pending|ready|failed
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)

    owner: Mapped[User] = relationship(back_populates="datasets")
    sheets: Mapped[list["Sheet"]] = relationship(back_populates="dataset",
                                                 cascade="all, delete-orphan")


class Sheet(Base):
    __tablename__ = "sheets"
    __table_args__ = (UniqueConstraint("dataset_id", "name", name="uq_sheet_name"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    dataset_id: Mapped[str] = mapped_column(String(32), ForeignKey("datasets.id", ondelete="CASCADE"),
                                            index=True)
    name: Mapped[str] = mapped_column(String(255))
    position: Mapped[int] = mapped_column(Integer, default=0)
    n_rows: Mapped[int] = mapped_column(Integer, default=0)
    n_cols: Mapped[int] = mapped_column(Integer, default=0)
    parquet_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    profile_json: Mapped[str] = mapped_column(Text, default="{}")
    types_json: Mapped[str] = mapped_column(Text, default="{}")

    dataset: Mapped[Dataset] = relationship(back_populates="sheets")
    columns: Mapped[list["ColumnMeta"]] = relationship(back_populates="sheet",
                                                       cascade="all, delete-orphan")


class ColumnMeta(Base):
    __tablename__ = "columns_meta"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    sheet_id: Mapped[str] = mapped_column(String(32), ForeignKey("sheets.id", ondelete="CASCADE"),
                                          index=True)
    name: Mapped[str] = mapped_column(String(255))
    dtype: Mapped[str] = mapped_column(String(32))
    cardinality: Mapped[int] = mapped_column(Integer, default=0)
    null_frac: Mapped[float] = mapped_column(Float, default=0.0)
    samples_json: Mapped[str] = mapped_column(Text, default="[]")

    sheet: Mapped[Sheet] = relationship(back_populates="columns")


class Presentation(Base):
    __tablename__ = "presentations"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id", ondelete="CASCADE"),
                                          index=True)
    dataset_id: Mapped[str] = mapped_column(String(32), ForeignKey("datasets.id", ondelete="CASCADE"),
                                            index=True)
    sheet: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(255), default="")
    summary_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    slides: Mapped[list["Slide"]] = relationship(back_populates="presentation",
                                                 cascade="all, delete-orphan",
                                                 order_by="Slide.position")


class Slide(Base):
    __tablename__ = "slides"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    presentation_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("presentations.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    slide_key: Mapped[str] = mapped_column(String(64), default="")
    chart_type: Mapped[str] = mapped_column(String(64), default="")
    title: Mapped[str] = mapped_column(String(512), default="")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")

    presentation: Mapped[Presentation] = relationship(back_populates="slides")


class ForecastRow(Base):
    """One historical demand record for the Demand Forecasting (T7) feature.

    Scoped per user + category; sku/date/quantity is the whole input schema
    (see FORECASTING.md). No relationship object is defined back on User —
    this table is queried directly by (owner_id, category) like a dataset.
    """
    __tablename__ = "forecast_rows"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id", ondelete="CASCADE"),
                                          index=True)
    category: Mapped[str] = mapped_column(String(16), index=True)  # T7-F | T7-SFG | T7-R
    date: Mapped[datetime] = mapped_column(DateTime(timezone=False), index=True)
    sku: Mapped[str] = mapped_column(String(120), index=True)
    quantity: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Job(Base):
    """Async parse/profile/plan job — the SSE endpoint reads state from here."""
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(String(32), index=True)
    kind: Mapped[str] = mapped_column(String(32), default="upload")
    dataset_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sheet: Mapped[str | None] = mapped_column(String(255), nullable=True)
    state: Mapped[str] = mapped_column(String(32), default="queued")
    pct: Mapped[float] = mapped_column(Float, default=0.0)
    detail: Mapped[str] = mapped_column(String(512), default="")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now,
                                                 onupdate=now)
