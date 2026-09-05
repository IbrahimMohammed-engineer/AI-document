"""
Test configuration and shared fixtures for the backend test suite.

All fixtures use testcontainers to spin up real PostgreSQL+pgvector and Redis
containers for integration tests. The async engine runs Alembic's full
migration stack (up to the current head) against each ephemeral database.

Pytest configuration:
  - asyncio_mode = "auto" means every coroutine test function is run as async
    without needing @pytest.mark.asyncio on each one.
  - The database fixture is function-scoped so each test gets a clean schema.

Unit tests never request the container fixtures, so they run without Docker.
"""
from __future__ import annotations

import os
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# ─── Pytest-asyncio configuration ────────────────────────────────────────────
pytest_plugins = ["pytest_asyncio"]


def _generate_test_rsa_keypair() -> tuple[str, str]:
    """Generate a throwaway RSA-2048 PEM pair for RS256 JWT tests (Phase 16).

    Runs ONCE at conftest import — before any app module is imported — so
    every Settings() instantiation in the test process sees the same keys.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    return private_pem, public_pem


_JWT_PRIVATE_PEM, _JWT_PUBLIC_PEM = _generate_test_rsa_keypair()

# Set before ANY test module imports app code — app.core.security reads
# get_settings() at import time and requires JWT configuration. Phase 16:
# tests exercise the production RS256 path; PEMs are embedded with literal
# \n escapes exactly as operators write them in .env (config unescapes).
os.environ.setdefault(
    "JWT_SECRET_KEY",
    "test-secret-key-for-testing-only-not-used-in-production-must-be-long-enough",
)
os.environ.setdefault("JWT_ALGORITHM", "RS256")
os.environ.setdefault("JWT_PRIVATE_KEY", _JWT_PRIVATE_PEM.replace("\n", "\\n"))
os.environ.setdefault("JWT_PUBLIC_KEY", _JWT_PUBLIC_PEM.replace("\n", "\\n"))


def pytest_configure(config):
    config.addinivalue_line("markers", "integration: marks tests as integration tests")
    config.addinivalue_line("markers", "unit: marks tests as unit tests")
    config.addinivalue_line(
        "markers", "security: adversarial security suite (merge-blocking)"
    )
    config.addinivalue_line(
        "markers", "isolation_matrix: tenant isolation matrix (merge-blocking)"
    )


# ─── Database fixture (testcontainers) ───────────────────────────────────────

_POSTGRES_USER = "test"
_POSTGRES_PASSWORD = "test"
_POSTGRES_DB = "test"


@pytest.fixture(scope="session")
def postgres_container():
    """Start a PostgreSQL+pgvector container for the test session.

    Built on the core generic DockerContainer (the testcontainers-postgres
    helper package is broken on this version — see requirements-dev.txt).
    The container runs for the entire test session and is stopped when it ends.
    """
    try:
        from testcontainers.core.generic import DockerContainer
    except ImportError:
        pytest.skip("testcontainers not installed — run: pip install -r requirements-dev.txt")

    import sqlalchemy as sa

    container = (
        DockerContainer(image="pgvector/pgvector:pg16")
        .with_env("POSTGRES_USER", _POSTGRES_USER)
        .with_env("POSTGRES_PASSWORD", _POSTGRES_PASSWORD)
        .with_env("POSTGRES_DB", _POSTGRES_DB)
        .with_exposed_ports(5432)
        .with_command("--fsync=off")
    )
    with container as postgres:
        host = postgres.get_container_host_ip()
        port = postgres.get_exposed_port(5432)

        # Wait until Postgres actually accepts connections before yielding
        import time

        dsn = f"postgresql+psycopg2://{_POSTGRES_USER}:{_POSTGRES_PASSWORD}@{host}:{port}/{_POSTGRES_DB}"
        engine = sa.create_engine(dsn, pool_pre_ping=True)
        deadline = time.time() + 60
        while True:
            try:
                with engine.connect() as conn:
                    conn.execute(sa.text("SELECT 1"))
                break
            except Exception:
                if time.time() > deadline:
                    raise TimeoutError("Postgres test container did not become ready in 60s")
                time.sleep(1)
        engine.dispose()
        yield postgres


@pytest.fixture(scope="session")
def database_url(postgres_container):
    """Synchronous connection URL for Alembic migrations."""
    host = postgres_container.get_container_host_ip()
    port = postgres_container.get_exposed_port(5432)
    return (
        f"postgresql+psycopg2://{_POSTGRES_USER}:{_POSTGRES_PASSWORD}@{host}:{port}/{_POSTGRES_DB}"
    )


@pytest.fixture(scope="session")
def async_database_url(postgres_container):
    """Async connection URL for the SQLAlchemy async engine."""
    host = postgres_container.get_container_host_ip()
    port = postgres_container.get_exposed_port(5432)
    return (
        f"postgresql+asyncpg://{_POSTGRES_USER}:{_POSTGRES_PASSWORD}@{host}:{port}/{_POSTGRES_DB}"
    )


@pytest.fixture(scope="session")
def run_migrations(database_url):
    """Run Alembic migrations (upgrade head) against the test database.

    Runs once per test session, so migrations are only applied once and all
    tests share the same schema. Only started when a DB-backed fixture is
    requested (db_session / app fixtures) — unit tests stay Docker-free.
    """
    import subprocess
    import sys

    env = os.environ.copy()
    env["MIGRATION_DATABASE_URL"] = database_url

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env=env,
        capture_output=True,
        text=True,
        cwd=os.path.dirname(os.path.dirname(__file__)),
    )
    if result.returncode != 0:
        pytest.fail(
            f"Alembic upgrade failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        )


@pytest_asyncio.fixture()
async def db_session(async_database_url, run_migrations) -> AsyncGenerator[AsyncSession, None]:
    """Yield a fresh async DB session for each test.

    Uncommitted work is rolled back automatically at teardown.  Tests (or
    stage code under test) that COMMIT persist their rows — they seed with
    fresh UUIDs per test, and the integration files that need a pristine
    DB use the explicit table-cleanup fixtures instead.  (The previous
    SAVEPOINT-wrapper version broke on any test-internal commit: the
    savepoint is released with the transaction, so teardown raised
    ResourceClosedError.)
    """
    engine = create_async_engine(async_database_url, echo=False)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as session:
        yield session
        await session.rollback()

    await engine.dispose()


# ─── Settings override ────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def override_settings(monkeypatch):
    """Override settings so every test uses the test JWT secret.

    Deliberately does NOT depend on the database fixtures — unit tests must
    run without Docker. DB-backed tests point their own engines at the
    container URLs directly.

    Clears the lru_cache on get_settings() so fresh settings are loaded, and
    restores the cache afterwards.
    """
    monkeypatch.setenv(
        "JWT_SECRET_KEY",
        "test-secret-key-for-testing-only-not-used-in-production-must-be-long-enough",
    )

    # Clear the lru_cache so get_settings() returns fresh values
    from app.core.config import get_settings
    get_settings.cache_clear()

    yield

    # Restore after the test
    get_settings.cache_clear()


# ─── Redis fixture (testcontainers) ───────────────────────────────────────────

@pytest.fixture(scope="session")
def redis_container():
    """Start a Redis container for the test session (only when requested)."""
    try:
        from testcontainers.core.generic import DockerContainer
    except ImportError:
        pytest.skip("testcontainers not installed — run: pip install -r requirements-dev.txt")

    with DockerContainer("redis:7-alpine").with_exposed_ports(6379) as redis:
        yield redis


@pytest.fixture(scope="session")
def async_redis_url(redis_container) -> str:
    """Async Redis URL for the test container."""
    host = redis_container.get_container_host_ip()
    port = redis_container.get_exposed_port(6379)
    return f"redis://{host}:{port}/0"


@pytest_asyncio.fixture()
async def redis_client(async_redis_url) -> AsyncGenerator[aioredis.Redis, None]:  # type: ignore[type-arg]
    """Yield a fresh Redis client with a clean database for each test."""
    client = aioredis.from_url(async_redis_url, encoding="utf-8", decode_responses=True)
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


# ─── API app fixtures (httpx ASGI client with dependency overrides) ──────────

# FK-safe delete order for per-test cleanup. Roles/permissions are seeded by
# migration 002 and must survive — only tenant data is wiped. Citations and
# messages come FIRST: citations RESTRICT-delete against cited chunks/pages/
# versions (Phase 10 FK policy), so they must be gone before the document
# tables are wiped. Feedback/messages precede conversations (CASCADE would
# handle it, but explicit order documents the dependency chain).
_CLEANUP_TABLES = (
    "message_feedback",
    "citations",
    "messages",
    "conversation_documents",
    "conversations",
    "conflict_statements",
    "conflicts",
    "comparison_changes",
    "document_comparisons",
    # Phase 14 rows RESTRICT-delete against chunks/pages/versions/documents,
    # so they must be wiped before the document tables (items first).
    "document_extraction_items",
    "document_extractions",
    "document_summaries",
    "document_chunks",
    "document_sections",
    "document_pages",
    "processing_jobs",
    "collection_documents",
    "document_tags",
    "document_permissions",
    "collections",
    "document_versions",
    "documents",
    "audit_logs",
    "refresh_tokens",
    "user_roles",
    "users",
    "organizations",
)


@pytest_asyncio.fixture()
async def app_session_factory(async_database_url, run_migrations):
    """Session factory bound to the test database (for direct DB assertions)."""
    engine = create_async_engine(async_database_url, echo=False)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture()
async def app_client(app_session_factory, redis_client) -> AsyncGenerator[AsyncClient, None]:
    """httpx client driving the FastAPI app with test infrastructure injected.

    - get_db_session is overridden to the test Postgres (real migrations applied)
    - get_redis_client is overridden to the test Redis (flushed per test)
    - tenant tables are cleaned before and after each test
    """
    from httpx import ASGITransport, AsyncClient

    from app.api.deps import get_redis_client
    from app.infrastructure.database import get_db_session
    from app.main import app

    async def _clean_tables() -> None:
        async with app_session_factory() as session:
            for table in _CLEANUP_TABLES:
                await session.execute(text(f'DELETE FROM "{table}"'))
            await session.commit()

    await _clean_tables()

    async def _override_db() -> AsyncGenerator[AsyncSession, None]:
        async with app_session_factory() as session:
            yield session

    def _override_redis() -> aioredis.Redis:  # type: ignore[type-arg]
        return redis_client

    app.dependency_overrides[get_db_session] = _override_db
    app.dependency_overrides[get_redis_client] = _override_redis

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()
    await _clean_tables()
