"""
Integration tests — DocumentExtractionRepository (Phase 14, plan §8.2).

Covers: multiple runs per version persist as DISTINCT rows (append-only);
get_latest_completed_for_version correctness; add_items bulk-insert
correctness; category/item_index uniqueness; category-filtered listing;
and the tenant-isolation matrix entry.

Requires Docker (testcontainers).
"""
from __future__ import annotations


import pytest
from sqlalchemy.exc import IntegrityError

from app.models.extraction import DocumentExtractionItem
from app.repositories.document_extraction_repository import (
    DocumentExtractionRepository,
)

from tests.fixtures.extraction_fixtures import (
    seed_contract_document,
    seed_extraction_org,
)


def _item_row(category: str, index: int, doc: dict, org_id: str) -> dict:
    return {
        "category": category,
        "item_index": index,
        "label": f"{category} item {index}",
        "detail": {},
        "document_id": doc["document_id"],
        "document_version_id": doc["version_id"],
        "chunk_id": doc["chunk_ids"][0],
        "page_id": doc["page_id"],
        "page_number": 1,
        "section": "Parties",
        "quoted_text": "quoted source text",
        "char_start": 0,
        "char_end": 18,
        "relevance_score": 0.9,
    }


@pytest.mark.integration
class TestDocumentExtractionRepository:

    async def test_multiple_runs_persist_as_distinct_rows(
        self, db_session, app_session_factory
    ):
        org, user = await seed_extraction_org(app_session_factory)
        doc = await seed_contract_document(
            app_session_factory, organization_id=org.id, owner_id=user.id
        )
        repo = DocumentExtractionRepository(db_session)

        run_a = await repo.create(
            organization_id=org.id, document_id=doc["document_id"],
            document_version_id=doc["version_id"], schema_key="standard_v1",
            requested_by=user.id,
        )
        run_b = await repo.create(
            organization_id=org.id, document_id=doc["document_id"],
            document_version_id=doc["version_id"], schema_key="standard_v1",
            requested_by=user.id,
        )
        await db_session.commit()

        # Deliberately NOT unique per version — append-only audit history.
        assert run_a.id != run_b.id
        history = await repo.list_for_version(doc["version_id"])
        total = await repo.count_for_version(doc["version_id"])
        assert len(history) == 2
        assert total == 2

    async def test_latest_completed_for_version(
        self, db_session, app_session_factory
    ):
        org, user = await seed_extraction_org(app_session_factory)
        doc = await seed_contract_document(
            app_session_factory, organization_id=org.id, owner_id=user.id
        )
        repo = DocumentExtractionRepository(db_session)

        run_pending = await repo.create(
            organization_id=org.id, document_id=doc["document_id"],
            document_version_id=doc["version_id"], schema_key="standard_v1",
            requested_by=user.id,
        )
        await db_session.commit()
        assert await repo.get_latest_completed_for_version(doc["version_id"]) is None

        await repo.update_status(run_pending, "COMPLETED", model="stub-llm")
        await db_session.commit()

        # A newer PENDING run must not displace the completed one.
        await repo.create(
            organization_id=org.id, document_id=doc["document_id"],
            document_version_id=doc["version_id"], schema_key="standard_v1",
            requested_by=user.id,
        )
        await db_session.commit()

        latest = await repo.get_latest_completed_for_version(doc["version_id"])
        assert latest is not None
        assert latest.id == run_pending.id  # newer run is still PENDING

    async def test_add_items_bulk_insert_and_listing(
        self, db_session, app_session_factory
    ):
        org, user = await seed_extraction_org(app_session_factory)
        doc = await seed_contract_document(
            app_session_factory, organization_id=org.id, owner_id=user.id
        )
        repo = DocumentExtractionRepository(db_session)
        run = await repo.create(
            organization_id=org.id, document_id=doc["document_id"],
            document_version_id=doc["version_id"], schema_key="standard_v1",
            requested_by=user.id,
        )
        await db_session.commit()

        rows = [
            _item_row("requirement", 0, doc, org.id),
            _item_row("requirement", 1, doc, org.id),
            _item_row("party", 0, doc, org.id),
        ]
        inserted = await repo.add_items(run.id, rows)
        await db_session.commit()
        assert inserted == 3

        all_items = await repo.list_items(run.id)
        assert len(all_items) == 3

        reqs = await repo.list_items(run.id, category="requirement")
        assert [i.item_index for i in reqs] == [0, 1]

        empty = await repo.list_items(run.id, category="risk")
        assert empty == []

    async def test_duplicate_run_category_index_rejected(
        self, db_session, app_session_factory
    ):
        org, user = await seed_extraction_org(app_session_factory)
        doc = await seed_contract_document(
            app_session_factory, organization_id=org.id, owner_id=user.id
        )
        repo = DocumentExtractionRepository(db_session)
        run = await repo.create(
            organization_id=org.id, document_id=doc["document_id"],
            document_version_id=doc["version_id"], schema_key="standard_v1",
            requested_by=user.id,
        )
        await db_session.commit()
        await repo.add_items(run.id, [_item_row("date", 0, doc, org.id)])
        await db_session.commit()

        duplicate = DocumentExtractionItem(
            extraction_id=run.id, category="date", item_index=0,
            label="dup", document_id=doc["document_id"],
            document_version_id=doc["version_id"],
            chunk_id=doc["chunk_ids"][0], page_id=doc["page_id"],
            page_number=1, quoted_text="q",
        )
        db_session.add(duplicate)
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()

    async def test_tenant_isolation_never_cross_org(
        self, db_session, app_session_factory
    ):
        org_a, user_a = await seed_extraction_org(app_session_factory, slug="x-a")
        org_b, _user_b = await seed_extraction_org(app_session_factory, slug="x-b")
        doc = await seed_contract_document(
            app_session_factory, organization_id=org_a.id, owner_id=user_a.id
        )
        repo = DocumentExtractionRepository(db_session)
        run = await repo.create(
            organization_id=org_a.id, document_id=doc["document_id"],
            document_version_id=doc["version_id"], schema_key="standard_v1",
            requested_by=user_a.id,
        )
        await db_session.commit()

        assert await repo.get_by_id_for_org(run.id, org_b.id) is None
        assert await repo.get_by_id_for_org(run.id, org_a.id) is not None
