"""
Health check endpoints.

GET /health/live  — Process is running (always 200 if the app is up).
GET /health/ready — Application is ready to serve requests (200 if DB + Redis
                    are reachable; 503 otherwise).

These endpoints are intentionally outside any authentication middleware —
they are called by Docker healthchecks, load balancers, and CI smoke tests.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.infrastructure.database import check_db_health
from app.infrastructure.redis import check_redis_health

router = APIRouter()


@router.get("/health/live", summary="Liveness probe")
async def health_live() -> dict:
    """Return 200 if the process is running.

    A liveness probe failure causes the container orchestrator to restart the
    pod. It should only fail if the process itself is broken (deadlock, OOM),
    not if a dependency is temporarily unavailable.
    """
    return {"status": "ok"}


@router.get("/health/ready", summary="Readiness probe")
async def health_ready() -> JSONResponse:
    """Return 200 if the application can serve traffic.

    Checks PostgreSQL and Redis reachability. A readiness probe failure causes
    the container orchestrator to stop routing traffic to this instance (but
    not restart it) until it recovers.
    """
    db_ok = await check_db_health()
    redis_ok = await check_redis_health()

    status_detail = {
        "status": "ok" if (db_ok and redis_ok) else "degraded",
        "database": "ok" if db_ok else "unavailable",
        "redis": "ok" if redis_ok else "unavailable",
    }

    http_status = 200 if (db_ok and redis_ok) else 503
    return JSONResponse(content=status_detail, status_code=http_status)
