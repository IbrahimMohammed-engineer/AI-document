"""
Tenant isolation matrix (Phase 16, plan §6.3) — MERGE-BLOCKING.

Marker: ``isolation_matrix`` — CI must fail the merge when any scenario
here fails. Every access pattern asserts cross-tenant/ unauthorized reads
surface as 404 or as ABSENCE from retrieval — never 403 (which would
disclose resource existence) and never leaked content.

Matrix (plan §6.3):
  1. Org A user requests an Org B document            → 404
  2. Org A user searches with Org B chunks in the DB  → zero Org B results
  3. RESTRICTED doc in Org A, colleague WITHOUT grant → absent from retrieval
  4. RESTRICTED doc in Org A, colleague WITH grant    → visible
  5. RESTRICTED doc, colleague with EXPIRED grant     → absent
  6. PRIVATE doc owned by another org member          → absent (owner sees it)

Scenario 6.3's worker-tenancy row ("worker job with mismatched org →
FAILED") is verified by tests/integration/test_processing_jobs.py — the
tamper check lives in run_processing_job and is exercised there.
"""
from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.core.security import create_access_token
from app.infrastructure.embeddings import StubEmbeddingProvider, set_embedding_provider
from app.infrastructure.reranker import StubRerankerProvider, set_reranker_provider

pytestmark = [pytest.mark.isolation_matrix, pytest.mark.integration]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _uuid() -> str:
    return str(uuid.uuid4())


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _register_org(client) -> tuple[str, str, str]:
    """Register a fresh org+user. Returns (slug, token, user_id)."""
    slug = f"iso-{uuid.uuid4().hex[:8]}"
    resp = await client.post("/auth/register", json={
        "org_name": f"Isolation Org {slug}",
        "slug": slug,
        "email": f"{slug}@example.com",
        "password": "TestPassword123!",
        "full_name": "Isolation Tester",
    })
    assert resp.status_code == 201, resp.text
    login = await client.post("/auth/login", json={
        "email": f"{slug}@example.com",
        "password": "TestPassword123!",
        "org_slug": slug,
    })
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]
    me = await client.get("/auth/me", headers=_auth_headers(token))
    assert me.status_code == 200
    return slug, token, me.json()["id"]


async def _seed_user(
    app_session_factory, org_id: str, email: str, *, role_name: str = "Admin"
) -> str:
    """Insert a colleague user (with a system role) directly into the org."""
    user_id = _uuid()
    async with app_session_factory() as session:
        await session.execute(text(
            "INSERT INTO users (id, organization_id, email, full_name, is_active) "
            "VALUES (:id, :org, :email, :name, true)"
        ), {"id": user_id, "org": org_id, "email": email, "name": "Colleague"})
        await session.execute(text(
            "INSERT INTO user_roles (user_id, role_id, organization_id) "
            "SELECT :id, r.id, :org FROM roles r "
            "WHERE r.name = :role AND r.organization_id IS NULL"
        ), {"id": user_id, "org": org_id, "role": role_name})
        await session.commit()
    return user_id


def _token_for(user_id: str, org_id: str) -> str:
    return create_access_token(user_id, org_id=org_id)


async def _org_of(app_session_factory, user_id: str) -> str:
    async with app_session_factory() as session:
        row = await session.execute(
            text("SELECT organization_id FROM users WHERE id = :id"),
            {"id": user_id},
        )
        return str(row.scalar_one())


async def _seed_document(
    app_session_factory,
    org_id: str,
    owner_id: str,
    *,
    name: str,
    access_level: str = "organization",
    content: str = "The zebra retention policy requires seven year archival.",
) -> tuple[str, str]:
    """Seed an active document + READY version + page + one chunk."""
    doc_id, ver_id, page_id = _uuid(), _uuid(), _uuid()
    async with app_session_factory() as session:
        await session.execute(text(
            "INSERT INTO documents (id, organization_id, owner_id, name, "
            "document_type, status, access_level) "
            "VALUES (:id, :org, :owner, :name, 'policy', 'active', :access)"
        ), {"id": doc_id, "org": org_id, "owner": owner_id,
            "name": name, "access": access_level})
        await session.execute(text(
            "INSERT INTO document_versions (id, document_id, version_number, "
            "storage_key, mime_type, file_size_bytes, status, created_by) "
            "VALUES (:id, :doc, 1, 'key', 'application/pdf', 100, 'READY', :owner)"
        ), {"id": ver_id, "doc": doc_id, "owner": owner_id})
        await session.execute(text(
            "INSERT INTO document_pages (id, document_version_id, page_number, text) "
            "VALUES (:id, :ver, 1, 'page')"
        ), {"id": page_id, "ver": ver_id})
        embedder = StubEmbeddingProvider(dimensions=1536)
        vector = (await embedder.embed([content]))[0]
        vec_str = "[" + ",".join(str(x) for x in vector) + "]"
        await session.execute(text(
            "INSERT INTO document_chunks (id, document_version_id, organization_id, "
            "page_id, chunk_index, content, content_hash, token_count, embedding, "
            "embedding_model) VALUES (:id, :ver, :org, :page, 0, :content, :hash, "
            "12, CAST(:v AS vector), 'stub')"
        ), {"id": _uuid(), "ver": ver_id, "org": org_id, "page": page_id,
            "content": content, "hash": _uuid(), "v": vec_str})
        await session.commit()
    return doc_id, ver_id


async def _grant(
    app_session_factory,
    org_id: str,
    doc_id: str,
    user_id: str,
    *,
    expires_in: timedelta | None = None,
) -> None:
    expires_at = (
        f"(now() + interval '{int(expires_in.total_seconds())} seconds')"
        if expires_in is not None else "NULL"
    )
    async with app_session_factory() as session:
        await session.execute(text(
            "INSERT INTO document_permissions (id, organization_id, document_id, "
            "user_id, permission_type, granted_by, expires_at) "
            "VALUES (:id, :org, :doc, :user, 'read', :user, "
            f"{expires_at})"
        ), {"id": _uuid(), "org": org_id, "doc": doc_id, "user": user_id})
        await session.commit()


async def _search_document_ids(client, token: str, query: str) -> list[str]:
    """Keyword-mode search (deterministic FTS) → the returned document ids."""
    resp = await client.post(
        "/search",
        json={"query": query, "mode": "keyword", "top_k": 25},
        headers=_auth_headers(token),
    )
    assert resp.status_code == 200, resp.text
    return [r["document_id"] for r in resp.json()["results"]]


@pytest_asyncio.fixture(autouse=True)
def _stub_providers():
    set_embedding_provider(StubEmbeddingProvider(dimensions=1536))
    set_reranker_provider(StubRerankerProvider())
    yield
    set_reranker_provider(None)
    set_embedding_provider(None)


# ── The matrix ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestTenantIsolationMatrix:

    async def test_cross_org_document_get_is_404(
        self, app_client, app_session_factory
    ):
        """Scenario 1: Org A user fetching an Org B document gets a hard
        404 — never 403 (no existence disclosure)."""
        slug_a, token_a, user_a = await _register_org(app_client)
        _slug_b, _token_b, user_b = await _register_org(app_client)
        org_b = await _org_of(app_session_factory, user_b)
        del slug_a

        doc_b, _ = await _seed_document(
            app_session_factory, org_b, user_b, name="Org B Secret Policy"
        )

        resp = await app_client.get(
            f"/documents/{doc_b}", headers=_auth_headers(token_a)
        )
        assert resp.status_code == 404

    async def test_cross_org_chunks_not_in_search(
        self, app_client, app_session_factory
    ):
        """Scenario 2: Org B chunks exist in the DB; Org A searches for the
        exact distinctive term — zero Org B results."""
        _slug_a, token_a, user_a = await _register_org(app_client)
        _slug_b, _token_b, user_b = await _register_org(app_client)
        org_b = await _org_of(app_session_factory, user_b)

        await _seed_document(
            app_session_factory, org_b, user_b,
            name="B Only Policy", content="Xyloquine zebra archival mandate.",
        )

        ids = await _search_document_ids(app_client, token_a, "Xyloquine zebra")
        assert ids == []

    async def test_restricted_doc_not_in_retrieval_without_grant(
        self, app_client, app_session_factory
    ):
        """Scenario 3: a same-org colleague WITHOUT a grant cannot retrieve
        a RESTRICTED document."""
        _slug, _token_owner, user_owner = await _register_org(app_client)
        org = await _org_of(app_session_factory, user_owner)
        colleague = await _seed_user(
            app_session_factory, org, f"colleague-{_uuid()[:8]}@example.com"
        )
        colleague_token = _token_for(colleague, org)

        doc_id, _ = await _seed_document(
            app_session_factory, org, user_owner,
            name="Restricted Comp Plan", access_level="restricted",
            content="The falcrest incentive tier details are confidential.",
        )

        ids = await _search_document_ids(app_client, colleague_token, "falcrest incentive")
        assert doc_id not in ids

    async def test_restricted_doc_visible_with_grant(
        self, app_client, app_session_factory
    ):
        """Scenario 4: the SAME document becomes retrievable once the owner
        grants explicit access (document_permissions row)."""
        _slug, _token_owner, user_owner = await _register_org(app_client)
        org = await _org_of(app_session_factory, user_owner)
        colleague = await _seed_user(
            app_session_factory, org, f"colleague-{_uuid()[:8]}@example.com"
        )
        colleague_token = _token_for(colleague, org)

        doc_id, _ = await _seed_document(
            app_session_factory, org, user_owner,
            name="Restricted Comp Plan", access_level="restricted",
            content="The falcrest incentive tier details are confidential.",
        )
        await _grant(app_session_factory, org, doc_id, colleague)

        ids = await _search_document_ids(app_client, colleague_token, "falcrest incentive")
        assert doc_id in ids

    async def test_expired_grant_not_honoured(
        self, app_client, app_session_factory
    ):
        """Scenario 5: a grant whose expires_at has passed is dormant."""
        _slug, _token_owner, user_owner = await _register_org(app_client)
        org = await _org_of(app_session_factory, user_owner)
        colleague = await _seed_user(
            app_session_factory, org, f"colleague-{_uuid()[:8]}@example.com"
        )
        colleague_token = _token_for(colleague, org)

        doc_id, _ = await _seed_document(
            app_session_factory, org, user_owner,
            name="Restricted Audit Notes", access_level="restricted",
            content="Quartzline audit findings require restricted handling.",
        )
        await _grant(
            app_session_factory, org, doc_id, colleague,
            expires_in=timedelta(seconds=-1),  # already expired
        )

        ids = await _search_document_ids(app_client, colleague_token, "quartzline audit")
        assert doc_id not in ids

    async def test_private_doc_only_owner_visible(
        self, app_client, app_session_factory
    ):
        """Scenario 6: a PRIVATE document is invisible to a same-org
        non-owner and visible to its owner."""
        _slug, token_owner, user_owner = await _register_org(app_client)
        org = await _org_of(app_session_factory, user_owner)
        colleague = await _seed_user(
            app_session_factory, org, f"colleague-{_uuid()[:8]}@example.com"
        )
        colleague_token = _token_for(colleague, org)

        doc_id, _ = await _seed_document(
            app_session_factory, org, user_owner,
            name="Private Perf Reviews", access_level="private",
            content="Periwinkle personal review notes for managers only.",
        )

        colleague_ids = await _search_document_ids(
            app_client, colleague_token, "periwinkle review"
        )
        assert doc_id not in colleague_ids

        owner_ids = await _search_document_ids(
            app_client, token_owner, "periwinkle review"
        )
        assert doc_id in owner_ids

    async def test_restricted_grant_management_requires_owner_or_admin(
        self, app_client, app_session_factory
    ):
        """Boundary of the matrix: only the owner or document:admin may
        manage grants — a plain same-org member gets 403, and a cross-org
        actor gets 404 (existence hidden)."""
        _slug, _token_owner, user_owner = await _register_org(app_client)
        org = await _org_of(app_session_factory, user_owner)
        colleague = await _seed_user(
            app_session_factory, org, f"colleague-{_uuid()[:8]}@example.com",
            role_name="Viewer",  # no document:admin
        )
        colleague_token = _token_for(colleague, org)

        doc_id, _ = await _seed_document(
            app_session_factory, org, user_owner,
            name="Restricted Roadmap", access_level="restricted",
        )

        # Same-org non-owner non-admin → 403 (membership itself is not secret)
        resp = await app_client.get(
            f"/documents/{doc_id}/permissions", headers=_auth_headers(colleague_token)
        )
        assert resp.status_code == 403

        # Cross-org user → 404 (the document's existence is not disclosed)
        _slug_b, token_b, user_b = await _register_org(app_client)
        resp = await app_client.get(
            f"/documents/{doc_id}/permissions", headers=_auth_headers(token_b)
        )
        assert resp.status_code == 404
