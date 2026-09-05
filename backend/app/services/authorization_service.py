"""
Authorization service — live permission checks and document-scope resolution.

Phase 2: check_permission() — re-validates permissions against the DB on
  every sensitive operation (not just JWT claims — Backend §13 layer 2).

Phase 7: resolve_allowed_documents() — returns the set of document_version
  IDs the authenticated user is permitted to search/retrieve.  This is the
  mandatory pre-retrieval step for all RAG queries (Backend §29):

    Every retrieval function receives a non-optional version_ids list and
    every query includes that list as a WHERE predicate.  An empty list means
    zero allowed documents — the retrieval is short-circuited, never broadened.

Access rules (Backend §15; DB §13):
  - ``access_level = 'organization'``:  all active org members may read.
  - ``access_level = 'restricted'``:   owner + explicit document_permissions
    grants (Phase 16 — grants with permission_type read/write/admin and a
    NULL or future ``expires_at`` confer access; expired grants are dormant).
  - ``access_level = 'private'``:      owner only.

Scope parameter (domain.versioning.VersionScope):
  - kind='all'         — every document the user can read in the org.
  - kind='documents'   — a specific subset of document IDs.
  - kind='collections' — all documents belonging to the named collections.

Returns: list[str] of document_version_id UUIDs (the ``is_current`` version
  of each allowed document, or empty list if none are accessible).
"""
from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import InsufficientPermissionsError, NotFoundError
from app.domain.permissions import get_user_permissions, has_permission
from app.domain.versioning import VersionScope, resolve_current_version
from app.models.user import User
from app.repositories.user_repository import UserRepository

logger = logging.getLogger(__name__)


class AuthorizationService:

    # ── Permission check (Phase 2) ────────────────────────────────────────────

    @staticmethod
    async def check_permission(
        *,
        user: User,
        permission_key: str,
        db: AsyncSession,
    ) -> User:
        fresh_user = await UserRepository(db).get_with_roles(user.id, user.organization_id)
        if fresh_user is None:
            raise InsufficientPermissionsError("You no longer have access to this resource.")
        if not has_permission(fresh_user.roles, permission_key):
            raise InsufficientPermissionsError("You do not have permission to perform this action.")
        return fresh_user

    @staticmethod
    def resolve_allowed_document_filter(user: User) -> dict[str, set[str]]:
        return {"permissions": get_user_permissions(user.roles)}

    @staticmethod
    async def _granted_document_ids(
        db: AsyncSession,
        user: User,
        document_ids: list[str],
    ) -> set[str]:
        """Return the subset of document_ids with a LIVE grant for the user.

        A grant is live when permission_type is read/write/admin and
        ``expires_at`` is NULL or in the future. One indexed query per
        request (Phase 16 plan §18 — the RESTRICTED lookup stays O(1) per
        document, < 1ms on the (document_id, user_id) index).
        """
        from sqlalchemy import func, or_, select

        from app.models.document import DocumentPermission

        stmt = select(DocumentPermission.document_id).where(
            DocumentPermission.document_id.in_(document_ids),
            DocumentPermission.user_id == user.id,
            DocumentPermission.permission_type.in_(["read", "write", "admin"]),
            or_(
                DocumentPermission.expires_at.is_(None),
                DocumentPermission.expires_at > func.now(),
            ),
        )
        result = await db.execute(stmt)
        return {row[0] for row in result.all()}

    @staticmethod
    async def _check_document_access_level(
        document: object, user: User, db: AsyncSession
    ) -> bool:
        """Return True if the user may read this document.

        Phase 16 access rule implementation (matches resolve_allowed_documents):
          - 'organization':  all org members.
          - 'restricted':   owner + live document_permissions grant.
          - 'private':      owner only.
        """
        access_level = getattr(document, "access_level", None)
        owner_id = getattr(document, "owner_id", None)
        if access_level == "organization":
            return True
        if access_level == "private":
            return owner_id == user.id
        if access_level == "restricted":
            if owner_id == user.id:
                return True
            granted = await AuthorizationService._granted_document_ids(
                db, user, [getattr(document, "id")]
            )
            return bool(granted)
        return False  # unknown access level — exclude defensively

    @staticmethod
    async def authorize_document_version(
        user: User,
        document_version_id: str,
        db: AsyncSession,
    ) -> tuple:
        """Authorize a user to access a specific document version.

        Used by the comparison API (§9.2, §13) to verify both sides of a
        comparison request. Returns (version, document) tuple on success.

        Access rules:
          - Cross-org version ID → NotFoundError (existence leakage prevention)
          - Unauthorized access level → NotFoundError (same as above)

        Raises:
            NotFoundError: when the version does not exist, belongs to a
                different organization, or the user cannot access the parent
                document (treated uniformly as 404, never 403, to prevent
                resource-existence leakage per project-wide convention).
        """
        from sqlalchemy import select
        from app.models.document import Document, DocumentVersion

        result = await db.execute(
            select(DocumentVersion).where(DocumentVersion.id == document_version_id)
        )
        version = result.scalar_one_or_none()

        if version is None:
            raise NotFoundError("Document version not found.")

        doc_result = await db.execute(
            select(Document).where(Document.id == version.document_id)
        )
        document = doc_result.scalar_one_or_none()

        # Cross-org or missing document → 404 (never 403)
        if document is None or document.organization_id != user.organization_id:
            raise NotFoundError("Document version not found.")

        # Soft-deleted or inactive documents
        if getattr(document, "deleted_at", None) is not None:
            raise NotFoundError("Document version not found.")

        # Access-level check — returns 404 for consistency (no leakage)
        if not await AuthorizationService._check_document_access_level(
            document, user, db
        ):
            raise NotFoundError("Document version not found.")

        return version, document

    # ── Document scope resolution (Phase 7) ───────────────────────────────────

    @staticmethod
    async def resolve_allowed_documents(
        user: User,
        db: AsyncSession,
        *,
        scope: VersionScope | None = None,
    ) -> list[str]:
        """Return the list of document_version_id strings the user may search.

        This is the Phase 7 retrieval-gate (Backend §29 — four-layer
        enforcement; layer 4 = retrieval level).  The output feeds directly
        into ``chunk_repo.semantic_search(version_ids=...)``.

        Empty return value = no documents in scope → the caller must
        short-circuit and return an empty result set, NEVER broadening the
        scope or searching without a filter.

        Args:
            user:  The authenticated user (org_id from the JWT; never from
                   request body — Backend §14).
            db:    Active session for document/version queries.
            scope: Optional VersionScope; defaults to VersionScope.all_documents()
                   (all org-accessible documents the user can read).

        Returns:
            List of version_id strings (may be empty).
        """
        if scope is None:
            scope = VersionScope.all_documents()

        org_id = user.organization_id

        # Import here to avoid circular imports (repository → model → repository)
        from app.models.document import Document, DocumentVersion
        from sqlalchemy import select

        # ── Step 1: collect in-scope documents ────────────────────────────
        if scope.kind == "all":
            stmt = select(Document).where(
                Document.organization_id == org_id,
                Document.deleted_at.is_(None),
                Document.status == "active",
            )
        elif scope.kind == "documents":
            if not scope.document_ids:
                return []
            stmt = select(Document).where(
                Document.organization_id == org_id,
                Document.deleted_at.is_(None),
                Document.status == "active",
                Document.id.in_(list(scope.document_ids)),
            )
        elif scope.kind == "collections":
            if not scope.collection_ids:
                return []
            from app.models.document import CollectionDocument
            stmt = (
                select(Document)
                .join(
                    CollectionDocument,
                    CollectionDocument.document_id == Document.id,
                )
                .where(
                    Document.organization_id == org_id,
                    Document.deleted_at.is_(None),
                    Document.status == "active",
                    CollectionDocument.collection_id.in_(list(scope.collection_ids)),
                )
                .distinct()
            )
        else:
            logger.error("Unknown VersionScope.kind=%r — returning empty scope", scope.kind)
            return []

        result = await db.execute(stmt)
        documents = list(result.scalars().all())

        if not documents:
            logger.debug(
                "resolve_allowed_documents: no active documents in scope for org=%s scope=%s",
                org_id, scope.kind,
            )
            return []

        # ── Step 2: access-level filtering ────────────────────────────────
        # Phase 16 access rule implementation:
        #   - 'organization':  all org members
        #   - 'private':       owner only
        #   - 'restricted':    owner + live document_permissions grants
        #     (single batched grant lookup for every restricted doc in
        #     scope — never one query per document)
        restricted_ids = [
            doc.id for doc in documents if doc.access_level == "restricted"
        ]
        granted_ids = (
            await AuthorizationService._granted_document_ids(
                db, user, restricted_ids
            )
            if restricted_ids
            else set()
        )

        allowed_doc_ids: list[str] = []
        for doc in documents:
            if doc.access_level == "organization":
                allowed_doc_ids.append(doc.id)
            elif doc.access_level == "private":
                if doc.owner_id == user.id:
                    allowed_doc_ids.append(doc.id)
                # else: not accessible — silently excluded (never 403 on search)
            elif doc.access_level == "restricted":
                # Phase 16: owner bypass or live explicit grant
                if doc.owner_id == user.id or doc.id in granted_ids:
                    allowed_doc_ids.append(doc.id)
                # else: no grant — RESTRICTED stays invisible (never 403)
            else:
                # Unknown access level — exclude defensively
                logger.warning(
                    "Document %s has unrecognised access_level=%r — excluding from search",
                    doc.id, doc.access_level,
                )

        if not allowed_doc_ids:
            return []

        # ── Step 3: resolve current versions ──────────────────────────────
        # Fetch all versions for the allowed documents in one query, then
        # use the pure domain function to pick the current one per doc.
        # Phase 8 (Backend §30): when the scope carries a temporal ``as_of``
        # point-in-time, the SAME domain function resolves "the version
        # effective at that moment" — no separate historical code path.
        versions_result = await db.execute(
            select(DocumentVersion).where(
                DocumentVersion.document_id.in_(allowed_doc_ids),
                DocumentVersion.status == "READY",
            )
        )
        all_versions = list(versions_result.scalars().all())

        as_of_date = _parse_as_of(scope.as_of)
        if scope.as_of and as_of_date is None:
            logger.warning(
                "resolve_allowed_documents: unparseable scope.as_of=%r — "
                "resolving current versions instead",
                scope.as_of,
            )

        # Group versions by document_id, resolve current for each
        from collections import defaultdict
        by_doc: dict[str, list] = defaultdict(list)
        for v in all_versions:
            by_doc[v.document_id].append(v)

        version_ids: list[str] = []
        for doc_id, versions in by_doc.items():
            current = resolve_current_version(versions, as_of=as_of_date)
            if current is not None:
                version_ids.append(str(getattr(current, "id")))
            # None → the document had no version effective at as_of: it is
            # excluded from this query's candidate set, NOT an error
            # (Backend §30 step 4).

        logger.debug(
            "resolve_allowed_documents: org=%s scope=%s as_of=%s → %d allowed documents, "
            "%d ready versions",
            org_id, scope.kind, scope.as_of, len(allowed_doc_ids), len(version_ids),
        )

        # ── Phase 16: RETRIEVAL_SCOPED audit — counts only, never IDs ─────
        # Deliberately written on a DEDICATED short-lived session: the
        # retrieval gate must never leave pending writes inside the
        # caller's transaction (read paths legitimately end without
        # committing, and a wedged transaction here would stall requests).
        try:
            from app.infrastructure.database import get_session_factory
            from app.services.audit_logger import AuditAction, AuditLogger

            factory = get_session_factory()
            async with factory() as audit_session:
                await AuditLogger.log(
                    audit_session,
                    organization_id=org_id,
                    user_id=user.id,
                    action=AuditAction.RETRIEVAL_SCOPED,
                    resource_type="retrieval_scope",
                    resource_id=None,
                    metadata={
                        "version_count": len(version_ids),
                        "scope_kind": scope.kind,
                    },
                )
                await audit_session.commit()
        except Exception:  # noqa: BLE001 — audit must never break retrieval
            logger.warning("RETRIEVAL_SCOPED audit write failed — continuing")

        return version_ids


def _parse_as_of(as_of: str | None):
    """Parse the scope's ISO-8601 ``as_of`` string into a date (or None)."""
    if not as_of:
        return None
    from datetime import date as _date, datetime as _datetime

    try:
        parsed = _datetime.fromisoformat(as_of.replace("Z", "+00:00"))
        return parsed.date()
    except ValueError:
        try:
            return _date.fromisoformat(as_of[:10])
        except ValueError:
            return None
