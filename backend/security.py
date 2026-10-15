"""Password hashing, JWTs, refresh-token rotation, CSRF double-submit tokens."""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from models import RefreshToken, User

_ph = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)

ACCESS_COOKIE = "ds_access"
REFRESH_COOKIE = "ds_refresh"
CSRF_COOKIE = "ds_csrf"
CSRF_HEADER = "x-csrf-token"


# --- passwords -------------------------------------------------------------
def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password: str, hashed: str | None) -> bool:
    if not hashed:
        return False
    try:
        return _ph.verify(hashed, password)
    except VerifyMismatchError:
        return False
    except Exception:
        return False


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


# --- jwt -------------------------------------------------------------------
def _encode(payload: dict[str, Any]) -> str:
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def make_access_token(user_id: str, is_guest: bool = False) -> str:
    now = datetime.now(timezone.utc)
    return _encode({
        "sub": user_id, "typ": "access", "guest": is_guest,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.access_ttl_min)).timestamp()),
    })


def decode_token(token: str) -> dict[str, Any] | None:
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except Exception:
        return None


# --- refresh tokens (opaque, stored hashed, rotating) ----------------------
async def issue_refresh_token(session: AsyncSession, user_id: str) -> str:
    raw = secrets.token_urlsafe(48)
    session.add(RefreshToken(
        user_id=user_id,
        token_hash=sha256(raw),
        expires_at=datetime.now(timezone.utc) + timedelta(days=settings.refresh_ttl_days),
    ))
    await session.flush()
    return raw


async def rotate_refresh_token(session: AsyncSession, raw: str) -> tuple[User, str] | None:
    """Consume a refresh token and mint a replacement. None when invalid/expired."""
    row = (await session.execute(
        select(RefreshToken).where(RefreshToken.token_hash == sha256(raw))
    )).scalar_one_or_none()
    if row is None or row.revoked:
        return None
    exp = row.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if exp < datetime.now(timezone.utc):
        return None
    user = (await session.execute(select(User).where(User.id == row.user_id))).scalar_one_or_none()
    if user is None:
        return None
    row.revoked = True
    new_raw = await issue_refresh_token(session, user.id)
    return user, new_raw


async def revoke_all(session: AsyncSession, user_id: str) -> None:
    rows = (await session.execute(
        select(RefreshToken).where(RefreshToken.user_id == user_id,
                                   RefreshToken.revoked == False)  # noqa: E712
    )).scalars().all()
    for r in rows:
        r.revoked = True


# --- csrf ------------------------------------------------------------------
def new_csrf_token() -> str:
    return secrets.token_urlsafe(24)


def csrf_ok(cookie_value: str | None, header_value: str | None) -> bool:
    return bool(cookie_value and header_value and secrets.compare_digest(cookie_value,
                                                                        header_value))
