"""Rate limiting via slowapi. Redis-backed when REDIS_URL is set, in-memory otherwise."""
from __future__ import annotations

from fastapi import Request
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from config import settings


def user_or_ip(request: Request) -> str:
    """Per-user key when authenticated (set by the auth dependency), else per-IP."""
    uid = getattr(request.state, "user_id", None)
    if uid:
        return f"user:{uid}"
    auth = request.headers.get("authorization")
    if auth:
        return f"tok:{auth[-24:]}"
    return f"ip:{get_remote_address(request)}"


limiter = Limiter(
    key_func=user_or_ip,
    default_limits=[settings.rate_api],
    storage_uri=settings.redis_url if settings.redis_url else "memory://",
    enabled=settings.rate_limit_enabled,
    headers_enabled=False,
)


def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=429,
        content={"detail": f"Rate limit exceeded ({exc.detail}). Please slow down and retry."},
    )
