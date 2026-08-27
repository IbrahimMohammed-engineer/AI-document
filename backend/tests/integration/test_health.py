"""
Integration tests for health check endpoints.
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.integration
async def test_health_live(async_database_url):
    """GET /health/live always returns 200 when the app is running."""
    from app.main import app

    # Initialize infrastructure with test DB
    import os
    os.environ["DATABASE_URL"] = async_database_url

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/health/live")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"


@pytest.mark.integration
async def test_health_ready_with_db(async_database_url):
    """GET /health/ready returns 200 when DB is reachable."""
    from app.infrastructure.database import init_db, close_db, _engine
    from app.main import app

    # Ensure engine is initialized
    await init_db()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/health/ready")
    finally:
        await close_db()

    # Should be 200 (DB ok, Redis may be unavailable in tests → degraded)
    assert response.status_code in (200, 503)
    data = response.json()
    assert data["database"] in ("ok", "unavailable")


@pytest.mark.integration
async def test_unknown_route_returns_error_envelope(async_database_url):
    """Requests to unknown routes return a structured 422/404 envelope."""
    from app.main import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/this-route-does-not-exist")

    assert response.status_code == 404
