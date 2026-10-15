"""Auth router + get_current_user dependency."""
from __future__ import annotations

import re
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from db import get_session
from models import ApiKey, User
from security import (ACCESS_COOKIE, CSRF_COOKIE, CSRF_HEADER, REFRESH_COOKIE,
                      csrf_ok, decode_token, hash_password, issue_refresh_token,
                      make_access_token, new_csrf_token, revoke_all,
                      rotate_refresh_token, sha256, verify_password)

router = APIRouter(prefix="/api/auth", tags=["auth"])

PUBLIC_PATHS = {"/api/health", "/api/ready", "/health", "/ready", "/api/config"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class Credentials(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)
    display_name: str | None = Field(default=None, max_length=120)


class TokenOut(BaseModel):
    user: dict
    access_token: str
    refresh_token: str
    csrf_token: str


def _cookie_kwargs() -> dict:
    same = settings.cookie_samesite.lower()
    secure = settings.cookie_secure or same == "none"
    return {"httponly": True, "secure": secure, "samesite": same, "path": "/"}


def set_auth_cookies(response: Response, access: str, refresh: str, csrf: str) -> None:
    ck = _cookie_kwargs()
    response.set_cookie(ACCESS_COOKIE, access, max_age=settings.access_ttl_min * 60, **ck)
    response.set_cookie(REFRESH_COOKIE, refresh, max_age=settings.refresh_ttl_days * 86400, **ck)
    # CSRF cookie is deliberately readable by JS (double-submit pattern).
    response.set_cookie(CSRF_COOKIE, csrf, max_age=settings.refresh_ttl_days * 86400,
                        httponly=False, secure=ck["secure"], samesite=ck["samesite"], path="/")


def clear_auth_cookies(response: Response) -> None:
    for name in (ACCESS_COOKIE, REFRESH_COOKIE, CSRF_COOKIE):
        response.delete_cookie(name, path="/")


def user_public(u: User) -> dict:
    return {"id": u.id, "email": u.email, "is_guest": u.is_guest,
            "display_name": u.display_name or (u.email.split("@")[0] if u.email else "Guest")}


async def _issue(session: AsyncSession, response: Response, user: User) -> dict:
    access = make_access_token(user.id, user.is_guest)
    refresh = await issue_refresh_token(session, user.id)
    csrf = new_csrf_token()
    set_auth_cookies(response, access, refresh, csrf)
    return {"user": user_public(user), "access_token": access, "refresh_token": refresh,
            "csrf_token": csrf}


# --------------------------------------------------------------------------- #
# dependency
# --------------------------------------------------------------------------- #
def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization") or ""
    m = re.match(r"^Bearer\s+(.+)$", header, re.I)
    return m.group(1).strip() if m else None


async def get_current_user(request: Request,
                           session: AsyncSession = Depends(get_session)) -> User:
    """Bearer token, then httpOnly cookie, then API key. CSRF enforced for cookie auth."""
    token = _bearer(request)
    via_cookie = False
    if not token and request.method in SAFE_METHODS:
        # EventSource cannot set headers, so SSE passes ?access_token=... instead.
        token = request.query_params.get("access_token")
    if not token:
        token = request.cookies.get(ACCESS_COOKIE)
        via_cookie = bool(token)

    if token:
        payload = decode_token(token)
        if payload and payload.get("typ") == "access":
            if via_cookie and request.method not in SAFE_METHODS:
                if not csrf_ok(request.cookies.get(CSRF_COOKIE),
                               request.headers.get(CSRF_HEADER)):
                    raise HTTPException(status_code=403, detail="CSRF token missing or invalid.")
            user = (await session.execute(
                select(User).where(User.id == payload.get("sub"))
            )).scalar_one_or_none()
            if user is not None:
                request.state.user_id = user.id
                return user

    api_key = request.headers.get("x-api-key")
    if api_key:
        row = (await session.execute(
            select(ApiKey).where(ApiKey.key_hash == sha256(api_key))
        )).scalar_one_or_none()
        if row is not None:
            row.last_used_at = datetime.now(timezone.utc)
            user = (await session.execute(
                select(User).where(User.id == row.user_id))).scalar_one_or_none()
            if user is not None:
                request.state.user_id = user.id
                return user

    raise HTTPException(status_code=401, detail="Not authenticated.")


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #
@router.post("/signup")
async def signup(body: Credentials, response: Response,
                 session: AsyncSession = Depends(get_session)):
    email = body.email.lower()
    existing = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="That email is already registered — log in instead.")
    user = User(email=email, password_hash=hash_password(body.password),
                display_name=body.display_name, is_guest=False)
    session.add(user)
    await session.flush()
    return await _issue(session, response, user)


@router.post("/login")
async def login(body: Credentials, response: Response,
                session: AsyncSession = Depends(get_session)):
    email = body.email.lower()
    user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Email or password is incorrect.")
    return await _issue(session, response, user)


@router.post("/guest")
async def guest(response: Response, session: AsyncSession = Depends(get_session)):
    """Ephemeral user so the sample demo works instantly with no signup email."""
    if not settings.guest_mode:
        raise HTTPException(status_code=403, detail="Guest mode is disabled.")
    user = User(email=None, password_hash=None, is_guest=True, display_name="Guest")
    session.add(user)
    await session.flush()
    return await _issue(session, response, user)


@router.post("/refresh")
async def refresh(request: Request, response: Response,
                  body: dict = Body(default={}),
                  session: AsyncSession = Depends(get_session)):
    raw = (body or {}).get("refresh_token") or request.cookies.get(REFRESH_COOKIE)
    if not raw:
        raise HTTPException(status_code=401, detail="No refresh token supplied.")
    rotated = await rotate_refresh_token(session, raw)
    if rotated is None:
        clear_auth_cookies(response)
        raise HTTPException(status_code=401, detail="Session expired — please log in again.")
    user, new_raw = rotated
    access = make_access_token(user.id, user.is_guest)
    csrf = new_csrf_token()
    set_auth_cookies(response, access, new_raw, csrf)
    return {"user": user_public(user), "access_token": access, "refresh_token": new_raw,
            "csrf_token": csrf}


@router.post("/logout")
async def logout(request: Request, response: Response,
                 session: AsyncSession = Depends(get_session)):
    raw = request.cookies.get(REFRESH_COOKIE)
    token = _bearer(request) or request.cookies.get(ACCESS_COOKIE)
    payload = decode_token(token) if token else None
    if payload and payload.get("sub"):
        await revoke_all(session, str(payload["sub"]))
    elif raw:
        await rotate_refresh_token(session, raw)
    clear_auth_cookies(response)
    return {"ok": True}


@router.get("/me")
async def me(user: User = Depends(get_current_user)):
    return {"user": user_public(user)}
