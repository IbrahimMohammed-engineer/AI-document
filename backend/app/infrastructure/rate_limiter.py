"""
Redis-backed login rate limiting helpers.
"""
from __future__ import annotations

import math
import time

import redis.asyncio as aioredis

from app.core.exceptions import RateLimitExceededError


async def check_and_increment(
    redis: aioredis.Redis,  # type: ignore[type-arg]
    *,
    key: str,
    limit: int,
    window_seconds: int,
    message: str = "Too many requests. Please try again later.",
) -> int:
    now = time.time()
    member = f"{now}:{math.floor(now * 1000)}"
    pipeline = redis.pipeline()
    pipeline.zremrangebyscore(key, "-inf", now - window_seconds)
    pipeline.zcard(key)
    pipeline.zadd(key, {member: now})
    pipeline.expire(key, window_seconds)
    _, current_count, _, _ = await pipeline.execute()

    if int(current_count) >= limit:
        ttl = await redis.ttl(key)
        raise RateLimitExceededError(
            message,
            headers={"Retry-After": str(max(ttl, 1))},
        )

    return max(limit - int(current_count) - 1, 0)
