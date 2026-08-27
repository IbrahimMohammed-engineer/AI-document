"""
FastAPI application factory.

This module is the entry point for the FastAPI application. It:
  - Creates the FastAPI instance with metadata
  - Wires the lifespan context (startup/shutdown for DB, Redis)
  - Registers all routers
  - Registers exception handlers
  - Adds middleware (CORS, request ID / correlation logging)

Entrypoint for uvicorn: `uvicorn app.main:app`
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import RequestIdMiddleware, setup_logging
from app.infrastructure.database import check_db_health, close_db, init_db
from app.infrastructure.redis import check_redis_health, close_redis, init_redis
from app.infrastructure.storage import (
    create_storage_provider_from_settings,
    set_storage_provider,
)

settings = get_settings()

# Configure logging before anything else
setup_logging(log_level=settings.log_level)
logger = logging.getLogger(__name__)


# ─── Application lifespan ─────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage startup and shutdown of shared infrastructure resources.

    Resources are initialized in dependency order:
      Database → Redis → (Phase 3+: storage provider)

    Resources are disposed in reverse order at shutdown.
    """
    logger.info(
        "Starting AI Document Intelligence Platform",
        extra={"environment": settings.environment},
    )

    # ── Startup ──────────────────────────────────────────────────────────────
    await init_db()
    await init_redis()

    # Initialize object storage provider (Phase 3)
    try:
        storage_provider = create_storage_provider_from_settings()
        set_storage_provider(storage_provider)
        logger.info(
            "Object storage provider initialized",
            extra={"provider": settings.storage_provider},
        )
    except Exception as exc:
        logger.error(
            "Failed to initialize storage provider: %s",
            exc,
            extra={"provider": settings.storage_provider},
        )
        # Non-fatal on startup — uploads will fail with 503 if storage is down

    logger.info("All infrastructure initialized — application ready.")
    yield

    # ── Shutdown ─────────────────────────────────────────────────────────────
    logger.info("Shutting down — disposing infrastructure resources.")
    await close_redis()
    await close_db()
    logger.info("Shutdown complete.")


# ─── App factory ──────────────────────────────────────────────────────────────

def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="AI Document Intelligence Platform",
        description=(
            "Enterprise multi-tenant document intelligence — "
            "every AI answer is grounded in retrievable, citable source evidence."
        ),
        version="0.1.0",
        lifespan=lifespan,
        # Disable the auto-generated docs in production (enable via feature flag later)
        docs_url="/docs" if not settings.is_production else None,
        redoc_url="/redoc" if not settings.is_production else None,
    )

    # ── Middleware ────────────────────────────────────────────────────────────

    # CORS — must be added before other middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-Id"],
    )

    # Request ID / correlation ID (raw ASGI middleware — wraps everything)
    app.add_middleware(RequestIdMiddleware)  # type: ignore[arg-type]

    # ── Exception handlers ────────────────────────────────────────────────────
    register_exception_handlers(app)

    # ── Routers ───────────────────────────────────────────────────────────────
    # Health checks (always registered)
    from app.api import health
    app.include_router(health.router, tags=["health"])

    from app.api import auth
    app.include_router(auth.router)

    # Phase 3 — Documents + Collections
    from app.api.documents import collections_router, router as documents_router
    app.include_router(documents_router)
    app.include_router(collections_router)

    return app


# ─── Application instance ─────────────────────────────────────────────────────

app = create_app()
