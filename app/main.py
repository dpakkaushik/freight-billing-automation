"""FastAPI application entrypoint.

Wires:
  * /api/billing/* endpoints (invoices, LRs, generate, download)
  * /  (static UI)

The async worker is currently dormant because the only slow step
(portal submission) is parked behind the disabled "Submit to TMS Portal"
button. We'll re-enable it when we wire that path.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from jose import JWTError, jwt
from loguru import logger

from app import __version__
from app.api import billing as billing_api
from app.api import auth as auth_api
from app.api import dashboard as dashboard_api
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
app.include_router(billing_api.router)
app.include_router(dashboard_api.router)


@app.middleware("http")
async def capture_api_usage(request: Request, call_next):
    response = await call_next(request)
    user_id = None
    auth_header = request.headers.get("Authorization", "")
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
                path=request.url.path,
                method=request.method,
                status_code=response.status_code,
            )
            db.add(event)
            db.commit()
            record_api_event({
                "user_id": user_id,
                "path": request.url.path,
                "method": request.method,
                "status_code": response.status_code,
            })
    except Exception:
        logger.exception("Failed to record API usage event")
    return response


# --- Static UI ---
STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    """Serve the single-page UI."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/healthz")
def healthz():
    return {"status": "ok", "version": __version__}
