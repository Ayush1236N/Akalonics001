"""Central configuration — pydantic Settings, env driven, zero-infra defaults."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE / ".data")))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # --- core -------------------------------------------------------------
    debug: bool = Field(default=True, alias="DEBUG")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    app_name: str = "AkaLonics API"

    # --- persistence ------------------------------------------------------
    # Unset => SQLite in .data/akalonics.db (zero-infra dev mode).
    database_url: str | None = Field(default=None, alias="DATABASE_URL")
    redis_url: str | None = Field(default=None, alias="REDIS_URL")

    # --- storage ----------------------------------------------------------
    storage_backend: str = Field(default="local", alias="STORAGE_BACKEND")  # local | s3
    storage_dir: str = Field(default=str(DATA_DIR / "objects"), alias="STORAGE_DIR")
    s3_bucket: str | None = Field(default=None, alias="S3_BUCKET")
    s3_endpoint_url: str | None = Field(default=None, alias="S3_ENDPOINT_URL")
    s3_region: str = Field(default="us-east-1", alias="S3_REGION")
    s3_access_key_id: str | None = Field(default=None, alias="S3_ACCESS_KEY_ID")
    s3_secret_access_key: str | None = Field(default=None, alias="S3_SECRET_ACCESS_KEY")

    # --- auth -------------------------------------------------------------
    jwt_secret: str = Field(default="dev-insecure-change-me-please-set-JWT_SECRET-in-production", alias="JWT_SECRET")
    jwt_algorithm: str = "HS256"
    access_ttl_min: int = Field(default=15, alias="ACCESS_TTL_MIN")
    refresh_ttl_days: int = Field(default=30, alias="REFRESH_TTL_DAYS")
    cookie_secure: bool = Field(default=False, alias="COOKIE_SECURE")
    cookie_samesite: str = Field(default="none", alias="COOKIE_SAMESITE")  # none for cross-site preview

    # --- http -------------------------------------------------------------
    allowed_origins: str = Field(default="http://localhost:5173,http://localhost:4173",
                                 alias="ALLOWED_ORIGINS")
    max_upload_mb: int = Field(default=25, alias="MAX_UPLOAD_MB")

    # --- limits -----------------------------------------------------------
    rate_limit_enabled: bool = Field(default=True, alias="RATE_LIMIT_ENABLED")
    rate_uploads: str = Field(default="10/hour", alias="RATE_UPLOADS")
    rate_api: str = Field(default="100/minute", alias="RATE_API")
    rate_auth: str = Field(default="5/minute", alias="RATE_AUTH")

    # --- data guards ------------------------------------------------------
    max_rows: int = Field(default=500_000, alias="MAX_ROWS")
    max_cols: int = Field(default=512, alias="MAX_COLS")
    max_zip_members: int = Field(default=2_000, alias="MAX_ZIP_MEMBERS")
    max_uncompressed_mb: int = Field(default=400, alias="MAX_UNCOMPRESSED_MB")
    parquet_ttl_min: int = Field(default=180, alias="PARQUET_TTL_MIN")

    # --- observability ----------------------------------------------------
    sentry_dsn: str | None = Field(default=None, alias="SENTRY_DSN")

    # --- features ---------------------------------------------------------
    nl_charts_enabled: bool = Field(default=True, alias="NL_CHARTS_ENABLED")
    guest_mode: bool = Field(default=True, alias="GUEST_MODE")
    nl_model: str = Field(default="claude_sonnet_4_6", alias="NL_MODEL")
    use_worker: bool = Field(default=False, alias="USE_WORKER")

    # ---------------------------------------------------------------------
    @property
    def sqlalchemy_url(self) -> str:
        raw = self.database_url
        if not raw:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            return f"sqlite+aiosqlite:///{DATA_DIR / 'akalonics.db'}"
        if raw.startswith("postgres://"):
            raw = raw.replace("postgres://", "postgresql://", 1)
        if raw.startswith("postgresql://"):
            raw = raw.replace("postgresql://", "postgresql+asyncpg://", 1)
        if raw.startswith("sqlite:///"):
            raw = raw.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
        return raw

    @property
    def sync_sqlalchemy_url(self) -> str:
        """Alembic / worker sync engine URL."""
        url = self.sqlalchemy_url
        return url.replace("+aiosqlite", "").replace("+asyncpg", "+psycopg2")

    @property
    def is_sqlite(self) -> bool:
        return self.sqlalchemy_url.startswith("sqlite")

    @property
    def origins(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]

    @property
    def llm_available(self) -> bool:
        """NL charts degrade to the keyword matcher when no key is configured."""
        return bool(self.nl_charts_enabled and os.environ.get("ANTHROPIC_API_KEY"))


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
