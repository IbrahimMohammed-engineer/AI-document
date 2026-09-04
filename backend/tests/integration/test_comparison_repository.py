"""
Integration tests — DocumentComparisonRepository (Phase 12, plan §11 Task 5).

Covers the reuse-lookup (hit/miss + unique-constraint enforcement), the
status transitions with summary/error payloads, incremental add_change,
severity/section filtering on list_changes, the resume-supporting
list_done_sections, count_by_severity, and change-deletion CASCADE from
the comparison row.

Requires Docker (testcontainers) — follows the test_repositories.py
convention of testing repos directly against the real test DB.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.comparison import ComparisonChange, DocumentComparison
from app.models.document import Document, DocumentVersion
from app.models.organization import Organization
from app.models.user import User
from app.repositories.document_comparison_repository import (
    DocumentComparisonRepository,
)
from app.repositories.user_repository import OrganizationRepository, UserRepository


def _uuid() -> str:
    return str(uuid.uuid4())


@pytest.fixture()
async def seeded_org_user(db_session: AsyncSession):
    """Commit an org+user so FK targets exist; return (org_id, user_id)."""
    org = await OrganizationRepository(db_session).create(
        name="Comparison Repo Org", slug=f"cmp-repo-{uuid.uuid4().hex[:8]}"
    )
    user = await UserRepository(db_session).create(
        organization_id=org.id,
        email=f"admin@{org.slug}.test",
        full_name="Repo Tester",
        password_hash="$argon2id$test",
    )
    await db_session.commit()
    return str(org.id), str(user.id)


@pytest.fixture()
async def comparison_pair(
    db_session: AsyncSession, seeded_org_user
):
    """Org-scoped READY version pair (two rows in document_versions under one
    document) so comparisons can reference them."""
    org_id, user_id = seeded_org_user
    doc_id = _uuid()
    db_session.add(Document(
        id=doc_id, organization_id=org_id, owner_id=user_id,
        name="Repo Doc", document_type="policy", status="active",
    ))
    await db_session.flush()
    version_ids = []
    for n in (1, 2):
        vid = _uuid()
        db_session.add(DocumentVersion(
            id=vid, document_id=doc_id, version_number=n,
            storage_key="k", mime_type="application/pdf",
            file_size_bytes=1, created_by=user_id, status="READY",
        ))
        version_ids.append(vid)
    await db_session.commit()
    return org_id, user_id, version_ids


@pytest.mark.integration
class TestComparisonRepository:

    async def test_create_and_get_by_pair(
        self, db_session: AsyncSession, comparison_pair
    ):
        org_id, user_id, (ver_a, ver_b) = comparison_pair
        repo = DocumentComparisonRepository(db_session)

        created = await repo.create(
            organization_id=org_id, version_a_id=ver_a,
            version_b_id=ver_b, requested_by=user_id,
        )
        await db_session.commit()

        assert created.status == "PENDING"

        hit = await repo.get_by_pair(org_id, ver_a, ver_b)
        assert hit is not None and hit.id == created.id

        miss = await repo.get_by_pair(org_id, ver_b, ver_a)  # reversed order
        assert miss is None  # normalized order is part of the pair key

        other_org = await repo.get_by_pair(_uuid(), ver_a, ver_b)
        assert other_org is None  # tenant-scoped

    async def test_duplicate_pair_violates_unique_constraint(
        self, db_session: AsyncSession, comparison_pair
    ):
        org_id, user_id, (ver_a, ver_b) = comparison_pair
        repo = DocumentComparisonRepository(db_session)
        await repo.create(
            organization_id=org_id, version_a_id=ver_a,
            version_b_id=ver_b, requested_by=user_id,
        )
        await db_session.commit()

        # The unique pair constraint is the concurrency backstop (§9.3):
        # a second insert for the same normalized pair must fail.
        with pytest.raises(IntegrityError):
            await repo.create(
                organization_id=org_id, version_a_id=ver_a,
                version_b_id=ver_b, requested_by=user_id,
            )
        await db_session.rollback()

    async def test_update_status_transitions_with_summary(
        self, db_session: AsyncSession, comparison_pair
    ):
        org_id, user_id, (ver_a, ver_b) = comparison_pair
        repo = DocumentComparisonRepository(db_session)
        comparison = await repo.create(
            organization_id=org_id, version_a_id=ver_a,
            version_b_id=ver_b, requested_by=user_id,
        )
        await db_session.commit()

        await repo.update_status(comparison, "PROCESSING")
        await repo.update_status(
            comparison, "COMPLETED",
            summary={"total": 2, "major": 1, "moderate": 1, "minor": 0},
        )
        await db_session.commit()

        fetched = await repo.get_by_id(comparison.id)
        assert fetched.status == "COMPLETED"
        assert fetched.summary["total"] == 2
        assert fetched.completed_at is not None
        assert fetched.error_message is None

    async def test_add_change_incremental_and_filters(
        self, db_session: AsyncSession, comparison_pair
    ):
        org_id, user_id, (ver_a, ver_b) = comparison_pair
        repo = DocumentComparisonRepository(db_session)
        comparison = await repo.create(
            organization_id=org_id, version_a_id=ver_a,
            version_b_id=ver_b, requested_by=user_id,
        )
        await db_session.commit()

        await repo.add_change(
            comparison.id, change_type="MODIFIED", severity="MAJOR",
            section="3.1 Approval", old_chunk_id=None, new_chunk_id=None,
            old_text="old", new_text="new",
        )
        await repo.add_change(
            comparison.id, change_type="ADDED", severity="MINOR",
            section="5.1 New",
        )
        await repo.add_change(
            comparison.id, change_type="REMOVED", severity="MODERATE",
            section="2.0 Legacy",
        )
        await db_session.commit()

        everything = await repo.list_changes(comparison.id)
        assert len(everything) == 3

        majors = await repo.list_changes(comparison.id, severity="MAJOR")
        assert [c.section for c in majors] == ["3.1 Approval"]

        added = await repo.list_changes(comparison.id, section="5.1 New")
        assert len(added) == 1 and added[0].change_type == "ADDED"

        single = await repo.get_change(comparison.id, everything[0].id)
        assert single is not None and single.id == everything[0].id

        missing = await repo.get_change(comparison.id, _uuid())
        assert missing is None

    async def test_list_done_sections_and_counts(
        self, db_session: AsyncSession, comparison_pair
    ):
        org_id, user_id, (ver_a, ver_b) = comparison_pair
        repo = DocumentComparisonRepository(db_session)
        comparison = await repo.create(
            organization_id=org_id, version_a_id=ver_a,
            version_b_id=ver_b, requested_by=user_id,
        )
        await db_session.commit()

        await repo.add_change(
            comparison.id, change_type="MODIFIED", severity="MAJOR", section="3.1",
        )
        await repo.add_change(
            comparison.id, change_type="ADDED", severity="MINOR", section=None,
        )
        await db_session.commit()

        done = await repo.list_done_sections(comparison.id)
        assert done == {"3.1"}  # NULL sections never enter the resume set

        counts = await repo.count_by_severity(comparison.id)
        assert counts == {"MAJOR": 1, "MODERATE": 0, "MINOR": 1}

    async def test_changes_cascade_on_comparison_delete(
        self, db_session: AsyncSession, comparison_pair
    ):
        org_id, user_id, (ver_a, ver_b) = comparison_pair
        repo = DocumentComparisonRepository(db_session)
        comparison = await repo.create(
            organization_id=org_id, version_a_id=ver_a,
            version_b_id=ver_b, requested_by=user_id,
        )
        await db_session.commit()
        await repo.add_change(
            comparison.id, change_type="MODIFIED", severity="MINOR", section="x",
        )
        await db_session.commit()
        change_ids = [c.id for c in await repo.list_changes(comparison.id)]
        assert change_ids

        await db_session.delete(comparison)
        await db_session.commit()

        surviving = await db_session.get(ComparisonChange, change_ids[0])
        assert surviving is None  # CASCADE removed the change rows
