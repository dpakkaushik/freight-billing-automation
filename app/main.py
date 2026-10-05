"""FastAPI application entrypoint.

Wires:
  * /api/billing/* endpoints (invoices, LRs, generate, download)
  * /  (static UI)

The async worker is currently dormant because the only slow step
(portal submission) is parked behind the disabled "Submit to TMS Portal"
button. We'll re-enable it when we wire that path.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from jose import JWTError, jwt
from loguru import logger

from app import __version__
from app.api import auth as auth_api
from app.api import dashboard as dashboard_api
from app.api import eee_taxi as eee_taxi_api
from app.api import eee_taxi_cost_centres as eee_taxi_cost_centres_api
from app.api import eee_taxi_rates as eee_taxi_rates_api
from app.config import settings
from app.database import SessionLocal, init_db
from app.models import ApiUsageEvent, User
from app.services.supabase import record_api_event
from app.utils.logging_config import setup_logging


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    logger.info("Starting Automation Bot v{}", __version__)
    settings.ensure_dirs()
    init_db()
    yield
    logger.info("Shutting down")


app = FastAPI(
    title="Pallia Trans Billing Automation",
    version=__version__,
    lifespan=lifespan,
)

app.include_router(auth_api.router)
if settings.billing_module_enabled:
    # Imported only when enabled: it pulls in OCR dependencies at import time.
    from app.api import billing as billing_api

    app.include_router(billing_api.router)
app.include_router(dashboard_api.router)
app.include_router(eee_taxi_api.router)
app.include_router(eee_taxi_rates_api.router)
app.include_router(eee_taxi_cost_centres_api.router)


# Usage events are written off the request path: each write is several database
# round trips, and the response should not wait for analytics.
_usage_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="usage-events")

# Requests that are not user activity and are not worth a database write.
_UNTRACKED_PATHS = ("/healthz", "/static/", "/favicon.ico")


def _record_usage(auth_header: str, path: str, method: str, status_code: int) -> None:
    user_id = None
    if auth_header.startswith("Bearer "):
        token = auth_header.split(" ", 1)[1]
        try:
            payload = jwt.decode(token, settings.auth_jwt_secret, algorithms=["HS256"])
            email = payload.get("sub")
            if email:
                with SessionLocal() as db:  # type: ignore[call-arg]
                    user = db.query(User).filter(User.email == email).one_or_none()
                    if user is not None and user.is_active:
                        user_id = user.id
        except Exception:
            user_id = None

    try:
        with SessionLocal() as db:  # type: ignore[call-arg]
            event = ApiUsageEvent(
                user_id=user_id,
                path=path,
                method=method,
                status_code=status_code,
            )
            db.add(event)
            db.commit()
            record_api_event({
                "user_id": user_id,
                "path": path,
                "method": method,
                "status_code": status_code,
            })
    except Exception:
        logger.exception("Failed to record API usage event")


@app.middleware("http")
async def capture_api_usage(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if request.method != "OPTIONS" and not path.startswith(_UNTRACKED_PATHS):
        _usage_executor.submit(
            _record_usage,
            request.headers.get("Authorization", ""),
            path,
            request.method,
            response.status_code,
        )
    return response


# --- Static UI ---
STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    """Serve the single-page UI."""
    # no-cache: the browser re-checks on every load, so UI updates appear without a hard refresh
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/healthz")
def healthz():
    return {"status": "ok", "version": __version__}
