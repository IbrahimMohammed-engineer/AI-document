"""
Async SQLAlchemy engine, session factory, and FastAPI session dependency.

Design decisions:
- Engine is created once at startup (via the lifespan context manager in main.py)
  and disposed at shutdown — not per-request.
- Sessions are request-scoped and yielded via the `get_db_session` dependency.
- Pool settings are configurable via environment variables (see core/config.py).
- No raw SQL strings leak out of this module — everything goes through the ORM
  or SQLAlchemy Core constructs, keeping the Repository layer testable.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy import text

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# Module-level singletons — initialized at startup, disposed at shutdown
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def create_engine() -> AsyncEngine:
    """Create the async SQLAlchemy engine with connection pooling.

    Called once at application startup.
    """
    settings = get_settings()
    engine = create_async_engine(
        settings.database_url,
        echo=settings.is_development,       # log SQL in dev only
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        pool_recycle=settings.db_pool_recycle,
        pool_pre_ping=True,                 # detect stale connections before use
    )
    logger.info(
        "Database engine created",
        extra={"pool_size": settings.db_pool_size, "max_overflow": settings.db_max_overflow},
    )
    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create the async session factory bound to the given engine."""
    return async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,  # avoid lazy-load errors after commit in async code
        autocommit=False,
        autoflush=False,
    )


async def init_db() -> None:
    """Initialize the engine and session factory.

    Called from the FastAPI lifespan startup handler.
    """
    global _engine, _session_factory
    _engine = create_engine()
    _session_factory = create_session_factory(_engine)
    logger.info("Database engine initialized.")


async def close_db() -> None:
    """Dispose the engine connection pool.

    Called from the FastAPI lifespan shutdown handler.
    """
    global _engine
    if _engine is not None:
        await _engine.dispose()
        logger.info("Database engine disposed.")
        _engine = None


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the session factory.

    Raises RuntimeError if `init_db()` has not been called yet.
    Used by background workers that need a session outside a request context.
    """
    if _session_factory is None:
        raise RuntimeError("Database not initialized — call init_db() first.")
    return _session_factory


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a request-scoped async database session.

    The session is automatically closed (and the transaction rolled back if
    not committed) when the request completes.

    Usage in a router:
        @router.get("/items")
        async def list_items(db: AsyncSession = Depends(get_db_session)):
            ...
    """
    if _session_factory is None:
        raise RuntimeError("Database not initialized.")

    async with _session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def check_db_health() -> bool:
    """Return True if the database is reachable, False otherwise.

    Used by the /health/ready endpoint.
    """
    if _engine is None:
        return False
    try:
        async with _engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        logger.warning("Database health check failed: %s", exc)
        return False
