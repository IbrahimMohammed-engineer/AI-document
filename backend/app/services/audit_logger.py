"""
Single write-path helper for immutable audit log entries.
"""
from __future__ import annotations

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.refresh_token_repository import AuditLogRepository


class AuditAction:
    # ── Authentication events ─────────────────────────────────────────────────
    USER_LOGIN = "USER_LOGIN"
    USER_CREATED = "USER_CREATED"
    LOGIN_FAILED = "LOGIN_FAILED"
    LOGOUT = "LOGOUT"
    PASSWORD_RESET_REQUESTED = "PASSWORD_RESET_REQUESTED"
    PASSWORD_RESET_COMPLETED = "PASSWORD_RESET_COMPLETED"
    PERMISSION_CHANGED = "PERMISSION_CHANGED"
    TOKEN_REVOKED_REUSE_DETECTED = "TOKEN_REVOKED_REUSE_DETECTED"

    # ── Document events (Phase 3) ─────────────────────────────────────────────
    DOCUMENT_UPLOADED = "DOCUMENT_UPLOADED"
    # DOCUMENT_VIEWED is session-debounced by the service — only emitted once
    # per (user, document, session) to avoid flooding the audit log on repeated
    # opens of the same document within a short window.
    DOCUMENT_VIEWED = "DOCUMENT_VIEWED"
    DOCUMENT_DOWNLOADED = "DOCUMENT_DOWNLOADED"
    DOCUMENT_DELETED = "DOCUMENT_DELETED"
    DOCUMENT_RESTORED = "DOCUMENT_RESTORED"
    DOCUMENT_METADATA_UPDATED = "DOCUMENT_METADATA_UPDATED"
    ACCESS_LEVEL_CHANGED = "ACCESS_LEVEL_CHANGED"
    OWNERSHIP_TRANSFERRED = "OWNERSHIP_TRANSFERRED"
    DOCUMENT_VERSION_UPLOADED = "DOCUMENT_VERSION_UPLOADED"

    # ── Collection events (Phase 3) ───────────────────────────────────────────
    COLLECTION_CREATED = "COLLECTION_CREATED"
    COLLECTION_DELETED = "COLLECTION_DELETED"
    COLLECTION_DOCUMENT_ADDED = "COLLECTION_DOCUMENT_ADDED"
    COLLECTION_DOCUMENT_REMOVED = "COLLECTION_DOCUMENT_REMOVED"

    # ── Processing events (Phase 4) ───────────────────────────────────────────
    PROCESSING_RETRIED = "PROCESSING_RETRIED"

    # ── AI/chat events (Phase 11) ─────────────────────────────────────────────
    # Logged once the USER message is persisted — independent of whether
    # generation ultimately succeeds (Backend Flow 4 step 19).  Provider
    # payloads (the question text) are NEVER in the metadata (Backend §54).
    QUESTION_ASKED = "QUESTION_ASKED"

    # ── Conflict events (Phase 13) ────────────────────────────────────────────
    # Written exactly once, inside ConflictService.resolve().  Metadata
    # carries only {"decision", "note"} — never provider payloads or full
    # statement text (same small/non-sensitive metadata convention).
    CONFLICT_RESOLVED = "CONFLICT_RESOLVED"

    # ── Summary/extraction events (Phase 14) ──────────────────────────────────
    # Metadata carries only version/job ids — never prompt payloads or
    # summary/extraction content (same small/non-sensitive convention).
    SUMMARY_REGENERATED = "SUMMARY_REGENERATED"
    EXTRACTION_RUN_CREATED = "EXTRACTION_RUN_CREATED"

    # ── Security events (Phase 16) ────────────────────────────────────────────
    # INVARIANT (Backend §54): NO provider payload — prompt text, question
    # text, answer text, or document text — may ever appear in the metadata
    # of any of these events. Counts, ids, and enumerated outcomes only.
    # Canary hit post-generation — {"canary_hit": true, "model": ...}
    INJECTION_ATTEMPT_DETECTED = "INJECTION_ATTEMPT_DETECTED"
    # Retrieval scope resolved — {"version_count": N, "scope_kind": ...}
    # (counts only — never the version IDs themselves)
    RETRIEVAL_SCOPED = "RETRIEVAL_SCOPED"
    # Answer outcome — {"groundedness": ..., "citations": N}
    QUESTION_ANSWERED = "QUESTION_ANSWERED"
    # Explicit RESTRICTED-document grants
    DOCUMENT_PERMISSION_GRANTED = "DOCUMENT_PERMISSION_GRANTED"
    DOCUMENT_PERMISSION_REVOKED = "DOCUMENT_PERMISSION_REVOKED"
    # Retention cron hard-purge — {"version_count": N, "reason": "retention"}
    DOCUMENT_HARD_PURGED = "DOCUMENT_HARD_PURGED"
    # Algorithm-confusion attempt rejected — {"attempted_algorithm": "HS256"}
    JWT_ALGORITHM_DOWNGRADE_BLOCKED = "JWT_ALGORITHM_DOWNGRADE_BLOCKED"


class AuditLogger:
    @staticmethod
    async def log(
        db: AsyncSession,
        *,
        organization_id: str,
        user_id: str | None,
        action: str,
        resource_type: str,
        resource_id: str | None,
        metadata: dict | None = None,
        request: Request | None = None,
    ) -> None:
        repository = AuditLogRepository(db)
        await repository.create(
            organization_id=organization_id,
            user_id=user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            metadata=metadata,
            ip_address=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
