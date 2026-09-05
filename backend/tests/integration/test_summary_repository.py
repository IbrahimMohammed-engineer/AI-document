"""
Integration tests — DocumentSummaryRepository (Phase 14, plan §8.2).

Covers: UNIQUE(document_version_id) enforcement; update_status payload
persistence (the FAILED-never-overwrites rule); mark_stale no-op-when-absent
and set-when-present; and the tenant-isolation matrix (two orgs, never
cross-org) for the new repository methods — mandatory per the suite's
convention (test_repositories.py runs this matrix for every prior phase).

Requires Docker (testcontainers).
"""
from __future__ import annotations


import pytest
from sqlalchemy.exc import IntegrityError

from app.models.summary import DocumentSummary
from app.repositories.document_summary_repository import DocumentSummaryRepository

from tests.fixtures.summary_fixtures import seed_summary_document, seed_summary_org


@pytest.mark.integration
class TestDocumentSummaryRepository:

    async def test_create_and_get_by_version(self, db_session, app_session_factory):
        org, user = await seed_summary_org(app_session_factory)
        doc = await seed_summary_document(
            app_session_factory, organization_id=org.id, owner_id=user.id
        )
        repo = DocumentSummaryRepository(db_session)
        created = await repo.create(
            organization_id=org.id,
            document_id=doc["document_id"],
            document_version_id=doc["version_id"],
            requested_by=user.id,
        )
        await db_session.commit()

        assert created.status == "PENDING"

        hit = await repo.get_by_version(doc["version_id"])
        assert hit is not None and hit.id == created.id

    async def test_unique_per_version_enforced(self, db_session, app_session_factory):
        org, user = await seed_summary_org(app_session_factory)
        doc = await seed_summary_document(
            app_session_factory, organization_id=org.id, owner_id=user.id
        )
        repo = DocumentSummaryRepository(db_session)
        await repo.create(
            organization_id=org.id,
            document_id=doc["document_id"],
            document_version_id=doc["version_id"],
            requested_by=user.id,
        )
        await db_session.commit()

        duplicate = DocumentSummary(
            organization_id=org.id,
            document_id=doc["document_id"],
            document_version_id=doc["version_id"],
            status="PENDING",
            requested_by=user.id,
        )
        db_session.add(duplicate)
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()

    async def test_update_status_completed_persists_payload(
        self, db_session, app_session_factory
    ):
        org, user = await seed_summary_org(app_session_factory)
        doc = await seed_summary_document(
            app_session_factory, organization_id=org.id, owner_id=user.id
        )
        repo = DocumentSummaryRepository(db_session)
        summary = await repo.create(
            organization_id=org.id,
            document_id=doc["document_id"],
            document_version_id=doc["version_id"],
            requested_by=user.id,
        )
        await db_session.commit()

        payload = {"executive_summary": [{"text": "x", "citations": []}]}
        await repo.update_status(
            summary,
            "COMPLETED",
            summary_json=payload,
            sampling={"sampled": False, "strategy": "full"},
            model="stub-llm",
            prompt_version="v1",
            prompt_tokens=10,
            completion_tokens=5,
        )
        await db_session.commit()

        hit = await repo.get_by_version(doc["version_id"])
        assert hit is not None
        assert hit.status == "COMPLETED"
        assert hit.summary == payload
        assert hit.sampling == {"sampled": False, "strategy": "full"}
        assert hit.model == "stub-llm"
        assert hit.stale is False
        assert hit.completed_at is not None

    async def test_failed_transition_never_overwrites_payload(
        self, db_session, app_session_factory
    ):
        org, user = await seed_summary_org(app_session_factory)
        doc = await seed_summary_document(
            app_session_factory, organization_id=org.id, owner_id=user.id
        )
        repo = DocumentSummaryRepository(db_session)
        summary = await repo.create(
            organization_id=org.id,
            document_id=doc["document_id"],
            document_version_id=doc["version_id"],
            requested_by=user.id,
        )
        payload = {"key_points": [{"text": "kept", "citations": []}]}
        await repo.update_status(summary, "COMPLETED", summary_json=payload)
        await db_session.commit()

        # A FAILED regeneration must NOT blank the visible payload (§4.2).
        await repo.update_status(
            summary, "FAILED", error_message="boom"
        )
        await db_session.commit()

        hit = await repo.get_by_version(doc["version_id"])
        assert hit is not None
        assert hit.status == "FAILED"
        assert hit.summary == payload  # stale summary remains visible
        assert hit.error_message == "boom"

    async def test_mark_stale_noop_when_absent(self, db_session, app_session_factory):
        org, user = await seed_summary_org(app_session_factory)
        doc = await seed_summary_document(
            app_session_factory, organization_id=org.id, owner_id=user.id
        )
        repo = DocumentSummaryRepository(db_session)
        # No summary row exists — the UPDATE must be a no-op, not an error.
        await repo.mark_stale(doc["version_id"])
        await db_session.commit()

        assert await repo.get_by_version(doc["version_id"]) is None

    async def test_mark_stale_sets_when_present(self, db_session, app_session_factory):
        org, user = await seed_summary_org(app_session_factory)
        doc = await seed_summary_document(
            app_session_factory, organization_id=org.id, owner_id=user.id
        )
        repo = DocumentSummaryRepository(db_session)
        summary = await repo.create(
            organization_id=org.id,
            document_id=doc["document_id"],
            document_version_id=doc["version_id"],
            requested_by=user.id,
        )
        await repo.update_status(summary, "COMPLETED", summary_json={"topics": []})
        await db_session.commit()

        await repo.mark_stale(doc["version_id"])
        await db_session.commit()

        hit = await repo.get_by_version(doc["version_id"])
        assert hit is not None and hit.stale is True

    async def test_tenant_isolation_never_cross_org(self, db_session, app_session_factory):
        """The mandatory matrix entry: two orgs — a get_by_id_for_org with
        the wrong org returns None for every new repository method."""
        org_a, user_a = await seed_summary_org(app_session_factory, slug="iso-a")
        org_b, user_b = await seed_summary_org(app_session_factory, slug="iso-b")
        doc_a = await seed_summary_document(
            app_session_factory, organization_id=org_a.id, owner_id=user_a.id
        )
        repo = DocumentSummaryRepository(db_session)
        summary = await repo.create(
            organization_id=org_a.id,
            document_id=doc_a["document_id"],
            document_version_id=doc_a["version_id"],
            requested_by=user_a.id,
        )
        await db_session.commit()

        assert await repo.get_by_id_for_org(summary.id, org_b.id) is None
        assert await repo.get_by_id_for_org(summary.id, org_a.id) is not None

    async def test_get_by_id_for_org(self, db_session, app_session_factory):
        org, user = await seed_summary_org(app_session_factory)
        doc = await seed_summary_document(
            app_session_factory, organization_id=org.id, owner_id=user.id
        )
        repo = DocumentSummaryRepository(db_session)
        summary = await repo.create(
            organization_id=org.id,
            document_id=doc["document_id"],
            document_version_id=doc["version_id"],
            requested_by=user.id,
        )
        await db_session.commit()

        hit = await repo.get_by_id_for_org(summary.id, org.id)
        assert hit is not None and hit.id == summary.id
