import os
from contextlib import asynccontextmanager

import redis
from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from .config import settings
from .database import engine, Base, ensure_schema
from .routers import upload, jobs, download, styles, preview


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    ensure_schema()
    yield


app = FastAPI(title="Captionator", lifespan=lifespan)

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

    healthy = all(v == "ok" for v in checks.values())
    response.status_code = 200 if healthy else 503
    return {"status": "ok" if healthy else "degraded", "checks": checks}
