"""structlog JSON logging with request IDs + optional Sentry."""
from __future__ import annotations

import logging
import sys
import uuid
from contextvars import ContextVar

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from config import settings

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


def _add_request_id(_logger, _name, event_dict):
    event_dict["request_id"] = request_id_var.get()
    return event_dict


_configured = False


def configure_logging() -> None:
    global _configured
    if _configured:
        return
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level, force=True)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _add_request_id,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str = "akalonics"):
    configure_logging()
    return structlog.get_logger(name)


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Assigns/propagates X-Request-ID and logs one JSON line per request."""

    def __init__(self, app: ASGIApp):
        super().__init__(app)
        self.log = get_logger("akalonics.http")

    async def dispatch(self, request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        token = request_id_var.set(rid)
        request.state.request_id = rid
        try:
            response = await call_next(request)
        except Exception:
            # Return a real response instead of re-raising so the outer CORS
            # middleware can still attach headers — otherwise a server error
            # surfaces in the browser as an opaque CORS failure.
            self.log.exception("request_failed", path=request.url.path, method=request.method)
            request_id_var.reset(token)
            return JSONResponse(
                {"detail": "Internal server error.", "request_id": rid},
                status_code=500,
                headers={"X-Request-ID": rid},
            )
        response.headers["X-Request-ID"] = rid
        if not request.url.path.startswith(("/api/health", "/api/ready", "/health", "/ready")):
            self.log.info("request", method=request.method, path=request.url.path,
                          status=response.status_code,
                          user=getattr(request.state, "user_id", None))
        request_id_var.reset(token)
        return response


def init_sentry() -> bool:
    """No-op when SENTRY_DSN is unset."""
    if not settings.sentry_dsn:
        return False
    try:
        import sentry_sdk
        sentry_sdk.init(dsn=settings.sentry_dsn, traces_sample_rate=0.1,
                        environment="debug" if settings.debug else "production")
        return True
    except Exception:  # noqa: BLE001
        return False
