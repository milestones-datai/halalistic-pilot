"""FastAPI application entrypoint for Halalistic.

Stage 1: factory + /health (with DB ping).
Stage 2: + auth + RBAC (custom OAuth2/OIDC per BRD §5.2, §7), slowapi rate limiter,
         refresh-token rotation with family-based reuse detection.
Stage 10: + signed-cookie SessionMiddleware for the internal admin/curator
          console at /admin/ui/* (Jinja2 server-rendered).
"""
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app.admin.deps import session_secret
from app.admin.templates_env import render_template
from app.admin.ui import router as admin_ui_router
from app.api.v1.router import api_router
from app.core.config import settings
from app.core.logging import configure_logging
from app.core.rate_limit import limiter
from app.db.session import engine
from app.web.discovery import router as web_discovery_router
from app.web.owner_ui import router as web_owner_router
from app.web.ui import router as web_ui_router

configure_logging(settings.log_level)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    await engine.dispose()


# ---- Test-only session-inject middleware (Stage 10 admin UI tests) ----
# Reads `X-Test-User-Id: <uuid>` and sets request.session["user_id"].
# Allows the test client to simulate a logged-in admin/curator without
# fighting with httpx/ASGITransport cookie-jar quirks. NEVER reaches
# production — guarded by settings.env == "test".
class _TestSessionInjectMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        uid = request.headers.get("x-test-user-id")
        if uid:
            request.session["user_id"] = uid
        return await call_next(request)


app = FastAPI(
    title=settings.project_name,
    version="0.11.0",
    description="Halalistic — halal restaurant discovery + deals marketplace (Houston pilot).",
    lifespan=lifespan,
)

# Rate limiter wiring.
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Test-only middleware (registered at app creation time because
# add_middleware is forbidden after the first request).
if settings.env == "test":
    app.add_middleware(_TestSessionInjectMiddleware)

# Signed-cookie session for the internal admin/curator console. Mounted
# here (not in the v1 API surface) because the consumer-facing app uses
# bearer tokens; mixing both into the same auth dependency would leak
# cookies to API clients and JWTs to UI requests.
app.add_middleware(
    SessionMiddleware,
    secret_key=session_secret(),
    session_cookie="halalistic_admin_session",
    same_site="lax",
    https_only=(settings.env != "development"),
    max_age=8 * 60 * 60,  # 8 hours
)

# All v1 routes mounted under settings.api_v1_prefix.
app.include_router(api_router, prefix=settings.api_v1_prefix)

# Internal admin/curator UI (Stage 10). Jinja2 server-rendered, RBAC-
# gated via the require_ui_role dependency. Kept outside the v1 API
# because it has a different auth model (signed cookies).
app.include_router(admin_ui_router)

# Consumer-facing web app (Stage 11). SSR via Jinja2 + HTMX. Shares
# the same signed-cookie session as the admin console, with role-
# aware nav so a diner never sees owner-only links and vice versa.
app.include_router(web_ui_router)
app.include_router(web_discovery_router)
app.include_router(web_owner_router)


# Friendly error handler for the admin UI. We need two behaviors that
# the default JSON HTTPException handler doesn't give us:
#   - 303 (redirect to /admin/ui/login) when an unauthenticated user
#     hits a guarded page. The default handler returns JSON, not a
#     Location header.
#   - HTML (not JSON) for 401/403 raised by UI deps.
# Registered for ALL paths so API clients still get JSON — we only
# render HTML when the request path is under /admin/ui.
async def _admin_ui_error_handler(request: Request, exc: HTTPException):
    # Web + admin UI both need HTML (redirect to login, friendly error
    # page). /api/v1/* and other JSON surfaces get the default JSON.
    is_ui = request.url.path.startswith(("/admin/ui", "/web", "/owner", "/account", "/restaurants", "/deals", "/"))
    if is_ui:
        if exc.status_code == status.HTTP_303_SEE_OTHER:
            # Respect the Location header if the raising dep set one
            # (e.g. /admin/ui/login for the admin guard). Otherwise
            # fall back to the consumer login page.
            target = (exc.headers or {}).get("Location") or "/web/login"
            return RedirectResponse(url=target, status_code=303)
        if exc.status_code in (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN):
            return HTMLResponse(
                content=render_template(
                    "error.html",
                    user=None,
                    queue={"pending_certs": 0, "pending_reviews": 0,
                           "flagged_reviews": 0, "pending_deals": 0},
                    active="", app_version="0.11.0", settings=settings,
                    detail=exc.detail if isinstance(exc.detail, str) else str(exc.detail),
                ),
                status_code=exc.status_code,
            )
    # JSON surface (/api/v1/*). Preserve the original detail shape
    # (string OR dict) so the API contract stays stable across stages.
    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
    )


app.add_exception_handler(HTTPException, _admin_ui_error_handler)


@app.get("/health", tags=["meta"])
async def health() -> JSONResponse:
    """Liveness + DB ping. 200 only if both app and DB are healthy; 503 otherwise."""
    db_ok = False
    db_error: str | None = None
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        db_ok = True
    except Exception as exc:  # noqa: BLE001 — health probe must never raise
        db_error = str(exc)

    payload = {
        "status": "ok" if db_ok else "degraded",
        "db": "ok" if db_ok else "error",
        "env": settings.env,
    }
    if db_error and not db_ok:
        payload["db_error"] = db_error

    return JSONResponse(
        status_code=200 if db_ok else 503,
        content=payload,
    )
