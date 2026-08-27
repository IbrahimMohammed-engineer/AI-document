"""
Redis client factory and health check.

Redis is used for:
  - Rate limiting (Phase 2)
  - Background job queue with Arq (Phase 4)
  - Search result caching (Phase 8+)

Design: Redis is disposable infrastructure — nothing stored here is the
sole copy of a business fact. If Redis is flushed, jobs re-enqueue from
processing_jobs rows and caches repopulate on next read.
"""
from __future__ import annotations

import logging

import redis.asyncio as aioredis

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_redis_client: aioredis.Redis | None = None  # type: ignore[type-arg]


async def init_redis() -> None:
    """Initialize the Redis connection pool.

    Called from the FastAPI lifespan startup handler.
    """
    global _redis_client
    settings = get_settings()
    _redis_client = aioredis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5,
    )
    logger.info("Redis client initialized.", extra={"redis_url": settings.redis_url})


async def close_redis() -> None:
    """Close the Redis connection pool.

    Called from the FastAPI lifespan shutdown handler.
    """
    global _redis_client
    if _redis_client is not None:
        await _redis_client.aclose()
        logger.info("Redis client closed.")
        _redis_client = None


def get_redis() -> aioredis.Redis:  # type: ignore[type-arg]
    """Return the Redis client.

    Raises RuntimeError if init_redis() has not been called.
    """
    if _redis_client is None:
        raise RuntimeError("Redis not initialized — call init_redis() first.")
    return _redis_client


async def check_redis_health() -> bool:
    """Return True if Redis is reachable, False otherwise.

    Used by the /health/ready endpoint.
    """
    if _redis_client is None:
        return False
    try:
        await _redis_client.ping()
        return True
    except Exception as exc:
        logger.warning("Redis health check failed: %s", exc)
        return False
