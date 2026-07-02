import logging
import os
from contextlib import asynccontextmanager

import redis
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from .config import settings
from .database import engine, Base, ensure_schema
from .logging_setup import configure_logging
from .routers import upload, jobs, download, styles, preview

configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    ensure_schema()
    logger.info(
        "API started: model=%s storage=%s", settings.whisper_model, settings.storage_path
    )
    yield


app = FastAPI(title="Captionator", lifespan=lifespan)

# Register the upload size middleware first (outermost), before CORSMiddleware, so that
# the 413 short-circuit response is wrapped by CORS and includes Access-Control-Allow-Origin.
@app.middleware("http")
async def enforce_upload_size(request: Request, call_next):
    # FastAPI parses the whole multipart body (spooling it to disk) before the
    # upload endpoint's code runs, so a size check inside the endpoint can't
    # stop an oversized body from landing on disk first. Rejecting here, off
    # the declared Content-Length, refuses honest oversized uploads before a
    # single body byte is read. Chunked/lying clients are caught by the
    # byte-count check in the upload endpoint, and nginx's client_max_body_size
    # (2000m) caps everyone regardless.
    if request.method == "POST" and request.url.path == "/api/upload":
        try:
            content_length = int(request.headers.get("content-length") or 0)
        except ValueError:
            content_length = 0
        if content_length > settings.max_upload_bytes:
            logger.warning(
                "upload rejected (too large): content_length=%d limit=%dMB",
                content_length, settings.max_upload_mb,
            )
            return JSONResponse(
                status_code=413,
                content={"detail": f"File too large. Limit is {settings.max_upload_mb} MB."},
            )
    return await call_next(request)


# Only enable CORS when explicit origins are configured. The default deployment is
# same-origin behind nginx, so no wildcard is needed (and a wildcard would let any
# site script the unauthenticated API on a user's behalf).
_cors_origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )


app.include_router(upload.router, prefix="/api")
app.include_router(jobs.router, prefix="/api")
app.include_router(download.router, prefix="/api")
app.include_router(styles.router, prefix="/api")
app.include_router(preview.router, prefix="/api")


# Reused across health probes; redis-py reconnects on its own if Redis restarts.
_redis_client = redis.from_url(
    settings.redis_url, socket_connect_timeout=2, socket_timeout=2
)


def _check_redis() -> None:
    """The Celery broker — without it, uploads enqueue nothing and jobs never run."""
    _redis_client.ping()


def _check_database() -> None:
    """The SQLite job store — a failed connect means every upload would 500."""
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))


def _check_storage() -> None:
    """Uploads/outputs must be writable — the usual culprit is a broken volume mount
    or, since the workload runs non-root, a chown that didn't take."""
    for path in (settings.upload_path, settings.output_path):
        if not os.access(path, os.W_OK):
            raise OSError(f"{path} is not writable")


@app.get("/api/health")
def health(response: Response):
    """Readiness probe (used by the Docker healthcheck): verify the dependencies an
    upload actually needs rather than just reporting that the process is up. Returns
    503 if any dependency is down so the container is marked unhealthy instead of
    falsely 'ok', and names the failing check to make triage quick."""
    checks = {}
    for name, probe in (
        ("redis", _check_redis),
        ("database", _check_database),
        ("storage", _check_storage),
    ):
        try:
            probe()
            checks[name] = "ok"
        except Exception as exc:
            checks[name] = f"error: {exc}"
            logger.warning("health check failed: %s: %s", name, exc)

    healthy = all(v == "ok" for v in checks.values())
    response.status_code = 200 if healthy else 503
    return {"status": "ok" if healthy else "degraded", "checks": checks}
