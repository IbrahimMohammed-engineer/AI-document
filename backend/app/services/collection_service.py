"""
CollectionService — collection lifecycle business logic.

Collections are named document groups scoped to an organization.
A document may belong to multiple collections (N:M).

All mutations emit audit events via AuditLogger.
"""
from __future__ import annotations

import logging

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError
from app.repositories.document_repository import (
    CollectionRepository,
    DocumentRepository,
)
from app.schemas.document import (
    CollectionCreate,
    CollectionListResponse,
    CollectionResponse,
)
from app.services.audit_logger import AuditAction, AuditLogger

logger = logging.getLogger(__name__)


class CollectionService:
    """Collection management business logic (all static methods)."""

    @staticmethod
    async def create_collection(
        *,
        organization_id: str,
        user_id: str,
        payload: CollectionCreate,
        db: AsyncSession,
        request: Request | None = None,
    ) -> CollectionResponse:
        col_repo = CollectionRepository(db)

        # Enforce unique name within org
        existing = await col_repo.get_by_name_for_org(organization_id, payload.name)
        if existing is not None:
            raise ConflictError(
                f"A collection named '{payload.name}' already exists in your organization."
            )

        # SELECTs above implicitly began the transaction — write and commit on it
        try:
            col = await col_repo.create(
                organization_id=organization_id,
                name=payload.name,
                description=payload.description,
            )
            await AuditLogger.log(
                db,
                organization_id=organization_id,
                user_id=user_id,
                action=AuditAction.COLLECTION_CREATED,
                resource_type="collection",
                resource_id=col.id,
                metadata={"name": col.name},
                request=request,
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise

        return CollectionResponse(
            id=col.id,
            organization_id=col.organization_id,
            name=col.name,
            description=col.description,
            created_at=col.created_at,
            updated_at=col.updated_at,
            document_count=0,
        )

    @staticmethod
    async def list_collections(
        *,
        organization_id: str,
        db: AsyncSession,
    ) -> CollectionListResponse:
        col_repo = CollectionRepository(db)
        cols = await col_repo.list_for_org(organization_id)

        items = [
            CollectionResponse(
                id=c.id,
                organization_id=c.organization_id,
                name=c.name,
                description=c.description,
                created_at=c.created_at,
                updated_at=c.updated_at,
            )
            for c in cols
        ]
        return CollectionListResponse(items=items, total=len(items))

    @staticmethod
    async def delete_collection(
        *,
        collection_id: str,
        organization_id: str,
        user_id: str,
        db: AsyncSession,
        request: Request | None = None,
    ) -> None:
        col_repo = CollectionRepository(db)

        col = await col_repo.get_by_id_for_org(collection_id, organization_id)
        if col is None:
            raise NotFoundError("Collection not found.")

        try:
            await col_repo.delete(col)
            await AuditLogger.log(
                db,
                organization_id=organization_id,
                user_id=user_id,
                action=AuditAction.COLLECTION_DELETED,
                resource_type="collection",
                resource_id=collection_id,
                metadata={"name": col.name},
                request=request,
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    @staticmethod
    async def add_document(
        *,
        collection_id: str,
        document_id: str,
        organization_id: str,
        user_id: str,
        db: AsyncSession,
        request: Request | None = None,
    ) -> None:
        col_repo = CollectionRepository(db)
        doc_repo = DocumentRepository(db)

        # Verify both entities belong to this org
        col = await col_repo.get_by_id_for_org(collection_id, organization_id)
        if col is None:
            raise NotFoundError("Collection not found.")

        doc = await doc_repo.get_by_id_for_org(document_id, organization_id)
        if doc is None:
            raise NotFoundError("Document not found.")

        try:
            member = await col_repo.add_document(collection_id, document_id)
            if member is not None:
                await AuditLogger.log(
                    db,
                    organization_id=organization_id,
                    user_id=user_id,
                    action=AuditAction.COLLECTION_DOCUMENT_ADDED,
                    resource_type="collection",
                    resource_id=collection_id,
                    metadata={"document_id": document_id},
                    request=request,
                )
            # Already-a-member is idempotent — commit either way
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    @staticmethod
    async def remove_document(
        *,
        collection_id: str,
        document_id: str,
        organization_id: str,
        user_id: str,
        db: AsyncSession,
        request: Request | None = None,
    ) -> None:
        col_repo = CollectionRepository(db)

        col = await col_repo.get_by_id_for_org(collection_id, organization_id)
        if col is None:
            raise NotFoundError("Collection not found.")

        try:
            removed = await col_repo.remove_document(collection_id, document_id)
            if removed:
                await AuditLogger.log(
                    db,
                    organization_id=organization_id,
                    user_id=user_id,
                    action=AuditAction.COLLECTION_DOCUMENT_REMOVED,
                    resource_type="collection",
                    resource_id=collection_id,
                    metadata={"document_id": document_id},
                    request=request,
                )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
