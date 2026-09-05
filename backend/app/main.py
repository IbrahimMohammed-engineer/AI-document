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
from app.infrastructure.database import close_db, init_db
from app.infrastructure.embeddings import init_embedding_provider
from app.infrastructure.llm import init_llm_provider
from app.infrastructure.queue import close_queue_pool, init_queue_pool
from app.infrastructure.reranker import init_reranker_provider
from app.infrastructure.redis import close_redis, init_redis
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

    # Arq enqueue pool (Phase 4 — upload creates jobs; workers consume them)
    try:
        await init_queue_pool()
    except Exception as exc:
        logger.error("Failed to initialize Arq queue pool: %s", exc)
        # Non-fatal — enqueues fail with logged errors; the worker sweep
        # recovers PENDING rows once Redis returns.

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

    # Initialize embedding provider (Phase 7)
    try:
        provider = init_embedding_provider()
        logger.info(
            "Embedding provider initialized",
            extra={
                "provider": settings.embedding_provider,
                "model": settings.embedding_model,
                "dimensions": settings.embedding_dimensions,
                "ready": provider is not None,
            },
        )
    except Exception as exc:
        logger.error(
            "Failed to initialize embedding provider: %s",
            exc,
            extra={"provider": settings.embedding_provider},
        )
        # Non-fatal on startup — search requests will fail with 503 if the
        # provider is misconfigured, but all other endpoints are unaffected.

    # Initialize reranker provider (Phase 8) — None (disabled) is a valid,
    # logged outcome: hybrid search then serves unreranked fused ordering.
    try:
        reranker = init_reranker_provider()
        logger.info(
            "Reranker provider initialized",
            extra={
                "provider": settings.reranker_provider,
                "model": settings.reranker_model,
                "reranking": reranker is not None,
            },
        )
    except Exception as exc:
        logger.error(
            "Failed to initialize reranker provider: %s",
            exc,
            extra={"provider": settings.reranker_provider},
        )
        # Non-fatal on startup — a reranker misconfiguration degrades
        # ordering (fused fallback), it never blocks the application.

    # Initialize LLM provider (Phase 9) — the /ask endpoint surfaces a
    # typed unavailable state when this is missing; startup stays unblocked.
    try:
        llm = init_llm_provider()
        logger.info(
            "LLM provider initialized",
            extra={
                "provider": settings.llm_provider,
                "model": settings.llm_model,
                "ready": llm is not None,
            },
        )
    except Exception as exc:
        logger.error(
            "Failed to initialize LLM provider: %s",
            exc,
            extra={"provider": settings.llm_provider},
        )
        # Non-fatal on startup — questions will fail with a typed,
        # retryable SSE error until the provider is configured.

    logger.info("All infrastructure initialized — application ready.")
    yield

    # ── Shutdown ─────────────────────────────────────────────────────────────
    logger.info("Shutting down — disposing infrastructure resources.")
    await close_queue_pool()
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

    # Phase 11 — processing SSE streams (registered BEFORE the documents
    # router so the multiplexed GET /documents/stream wins over the
    # /documents/{id} detail route)
    from app.api.document_streams import router as document_streams_router
    app.include_router(document_streams_router)

    # Phase 3 — Documents + Collections
    from app.api.documents import collections_router, router as documents_router
    app.include_router(documents_router)
    app.include_router(collections_router)

    # Phase 7/8 — Search (chunk debug tooling lives on the documents router)
    from app.api.search import router as search_router
    app.include_router(search_router)

    # Phase 9 — Ask AI (standalone RAG pipeline, SSE)
    from app.api.ask import router as ask_router
    app.include_router(ask_router)

    # Phase 11 — Conversations (persistent scoped streaming chat)
    from app.api.chat import router as chat_router
    app.include_router(chat_router)

    # Phase 12 — Document Comparison (versioning + comparison pipeline)
    from app.api.compare import router as compare_router
    app.include_router(compare_router)

    # Phase 13 — Conflict Detection (scanning, verification, resolution)
    from app.api.conflicts import router as conflicts_router
    app.include_router(conflicts_router)

    # Phase 14 — Document Summaries + Structured Extraction + minimal Analytics
    from app.api.summaries import router as summaries_router
    app.include_router(summaries_router)

    from app.api.extractions import documents_router as extraction_documents_router
    from app.api.extractions import router as extractions_router
    app.include_router(extractions_router)
    app.include_router(extraction_documents_router)

    from app.api.analytics import router as analytics_router
    app.include_router(analytics_router)

    # Phase 15 — Settings (profile, org, users, roles) + audit-log read
    from app.api.settings import audit_router, router as settings_router
    app.include_router(settings_router)
    app.include_router(audit_router)

    return app


# ─── Application instance ─────────────────────────────────────────────────────

app = create_app()
