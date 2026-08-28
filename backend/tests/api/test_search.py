"""
API tests for the search endpoints.

Phase 7: POST /search (semantic), GET /documents/{id}/chunks.
Phase 8: mode-toggle contract (hybrid/semantic/keyword), metadata filter
validation, temporal scope acceptance.

Uses the app_client fixture with real Postgres + Redis (testcontainers).
The StubEmbeddingProvider is injected so no real OpenAI calls are made.
The reranker process-global is left unset (None) in the API test process —
exactly the "reranking disabled" configuration, so hybrid/semantic modes
exercise the fallback presentation path.

Tests:
  - POST /search: unauthenticated → 401
  - POST /search: empty query → 422
  - POST /search: valid query with empty scope → empty results (no 4xx)
  - POST /search: invalid scope format → 422
  - POST /search (Phase 8): mode contract, invalid mode/document_type → 422,
    filters + as_of accepted, response carries mode/used_reranker
  - GET /documents/{id}/chunks: unauthenticated → 401
  - GET /documents/{id}/chunks: nonexistent doc → 404
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from app.infrastructure.embeddings import StubEmbeddingProvider, set_embedding_provider


# ── Helpers ───────────────────────────────────────────────────────────────────

def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _register_and_login(client) -> str:
    """Register a new org+user and return the access token."""
    org_slug = f"search-test-{uuid.uuid4().hex[:8]}"
    resp = await client.post("/auth/register", json={
        "org_name": f"Search Test Org {org_slug}",
        "slug": org_slug,
        "email": f"{org_slug}@example.com",
        "password": "TestPassword123!",
        "full_name": "Test User",
    })
    assert resp.status_code == 201, f"Registration failed: {resp.text}"

    login_resp = await client.post("/auth/login", json={
        "email": f"{org_slug}@example.com",
        "password": "TestPassword123!",
        "org_slug": org_slug,
    })
    assert login_resp.status_code == 200, f"Login failed: {login_resp.text}"
    return login_resp.json()["access_token"]


# ── Setup: inject stub provider for all tests ─────────────────────────────────

@pytest.fixture(autouse=True, scope="module")
def inject_stub_provider():
    """Ensure all tests use the stub embedding provider (no real API calls)."""
    set_embedding_provider(StubEmbeddingProvider(dimensions=1536))
    yield


# ── POST /search ──────────────────────────────────────────────────────────────

@pytest.mark.integration
class TestSearchEndpoint:

    @pytest.mark.asyncio
    async def test_unauthenticated_returns_401(self, app_client):
        resp = await app_client.post("/search", json={"query": "test query"})
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_empty_query_returns_422(self, app_client):
        token = await _register_and_login(app_client)
        resp = await app_client.post(
            "/search",
            json={"query": ""},
            headers=_auth_headers(token),
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_missing_query_returns_422(self, app_client):
        token = await _register_and_login(app_client)
        resp = await app_client.post(
            "/search",
            json={},
            headers=_auth_headers(token),
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_valid_query_empty_scope_returns_200(self, app_client):
        """User with no documents gets an empty but successful response."""
        token = await _register_and_login(app_client)
        resp = await app_client.post(
            "/search",
            json={"query": "what is the retention policy?"},
            headers=_auth_headers(token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "results" in body
        assert isinstance(body["results"], list)
        assert body["total"] == len(body["results"])
        assert body["query"] == "what is the retention policy?"
        assert body["scope_kind"] == "all"

    @pytest.mark.asyncio
    async def test_top_k_too_large_returns_422(self, app_client):
        token = await _register_and_login(app_client)
        resp = await app_client.post(
            "/search",
            json={"query": "anything", "top_k": 999},
            headers=_auth_headers(token),
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_query_too_long_returns_422(self, app_client):
        token = await _register_and_login(app_client)
        resp = await app_client.post(
            "/search",
            json={"query": "x" * 2001},
            headers=_auth_headers(token),
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_scope_documents_empty_list_defaults_to_all(self, app_client):
        """Empty document_ids list in scope should be treated as all-documents."""
        token = await _register_and_login(app_client)
        resp = await app_client.post(
            "/search",
            json={"query": "test", "scope": {"document_ids": []}},
            headers=_auth_headers(token),
        )
        # Should succeed (empty document list → all scope after stripping)
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_response_shape(self, app_client):
        """Verify the response schema matches our Pydantic model."""
        token = await _register_and_login(app_client)
        resp = await app_client.post(
            "/search",
            json={"query": "document policy", "top_k": 5},
            headers=_auth_headers(token),
        )
        assert resp.status_code == 200
        body = resp.json()
        # Mandatory envelope fields
        assert "results" in body
        assert "total" in body
        assert "query" in body
        assert "scope_kind" in body
        # total matches list length
        assert body["total"] == len(body["results"])


# ── POST /search — Phase 8 mode toggle + filters ──────────────────────────────

@pytest.mark.integration
class TestSearchPhase8Contract:

    @pytest.mark.asyncio
    async def test_default_mode_is_hybrid(self, app_client):
        token = await _register_and_login(app_client)
        resp = await app_client.post(
            "/search",
            json={"query": "retention policy"},
            headers=_auth_headers(token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["mode"] == "hybrid"
        # reranker global is unset in the test process → fallback ordering
        assert body["used_reranker"] is False

    @pytest.mark.asyncio
    async def test_keyword_mode_contract(self, app_client):
        token = await _register_and_login(app_client)
        resp = await app_client.post(
            "/search",
            json={"query": "retention policy", "mode": "keyword"},
            headers=_auth_headers(token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["mode"] == "keyword"
        assert body["used_reranker"] is False  # by design — keyword never reranks

    @pytest.mark.asyncio
    async def test_semantic_mode_contract(self, app_client):
        token = await _register_and_login(app_client)
        resp = await app_client.post(
            "/search",
            json={"query": "retention policy", "mode": "semantic"},
            headers=_auth_headers(token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["mode"] == "semantic"

    @pytest.mark.asyncio
    async def test_invalid_mode_returns_422(self, app_client):
        token = await _register_and_login(app_client)
        resp = await app_client.post(
            "/search",
            json={"query": "test", "mode": "bogus"},
            headers=_auth_headers(token),
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_unknown_document_type_returns_422(self, app_client):
        token = await _register_and_login(app_client)
        resp = await app_client.post(
            "/search",
            json={
                "query": "test",
                "filters": {"document_types": ["not-a-type"]},
            },
            headers=_auth_headers(token),
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_valid_filters_accepted(self, app_client):
        """Filters over an empty corpus → 200 with an empty result set."""
        token = await _register_and_login(app_client)
        resp = await app_client.post(
            "/search",
            json={
                "query": "retention",
                "mode": "keyword",
                "filters": {
                    "document_types": ["policy", "contract"],
                    "department": "Legal",
                },
            },
            headers=_auth_headers(token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["results"] == []
        assert body["total"] == 0

    @pytest.mark.asyncio
    async def test_temporal_scope_accepted(self, app_client):
        token = await _register_and_login(app_client)
        resp = await app_client.post(
            "/search",
            json={
                "query": "approval process",
                "scope": {"as_of": "2025-06-15"},
            },
            headers=_auth_headers(token),
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_invalid_temporal_scope_accepted_leniently(self, app_client):
        """as_of parsing is lenient downstream; the schema only bounds length."""
        token = await _register_and_login(app_client)
        resp = await app_client.post(
            "/search",
            json={
                "query": "approval process",
                "scope": {"as_of": "not-a-date"},
            },
            headers=_auth_headers(token),
        )
        assert resp.status_code == 200


# ── GET /documents/{id}/chunks ─────────────────────────────────────────────────

@pytest.mark.integration
class TestChunksEndpoint:

    @pytest.mark.asyncio
    async def test_unauthenticated_returns_401(self, app_client):
        fake_doc_id = str(uuid.uuid4())
        resp = await app_client.get(f"/documents/{fake_doc_id}/chunks")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_nonexistent_document_returns_404(self, app_client):
        token = await _register_and_login(app_client)
        fake_doc_id = str(uuid.uuid4())
        resp = await app_client.get(
            f"/documents/{fake_doc_id}/chunks",
            headers=_auth_headers(token),
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_invalid_uuid_returns_422(self, app_client):
        token = await _register_and_login(app_client)
        resp = await app_client.get(
            "/documents/not-a-uuid/chunks",
            headers=_auth_headers(token),
        )
        assert resp.status_code == 422
