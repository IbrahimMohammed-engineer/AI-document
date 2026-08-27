"""
Redis-backed single-use password reset token storage.
"""
from __future__ import annotations

import json

import redis.asyncio as aioredis


async def store(
    redis: aioredis.Redis,  # type: ignore[type-arg]
    *,
    token_hash: str,
    payload: dict,
    ttl_seconds: int = 3600,
) -> None:
    await redis.set(f"password-reset:{token_hash}", json.dumps(payload), ex=ttl_seconds)


async def consume(
    redis: aioredis.Redis,  # type: ignore[type-arg]
    *,
    token_hash: str,
) -> dict | None:
    raw = await redis.execute_command("GETDEL", f"password-reset:{token_hash}")
    if raw is None:
        return None
    return json.loads(raw)
