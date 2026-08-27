"""
Document domain logic — pure, unit-testable business rules.

Contains:
  - Enums: DocumentType, DocumentStatus, AccessLevel, VersionStatus
  - File validation: MIME allow-list, magic-byte sniff, size limit
  - Storage key computation (deterministic, org-namespaced)
  - Duplicate detection helper (SHA-256)

No I/O, no database access, no external calls — pure functions only.

See:
  Backend-Architecture-Documentation.md §17.1 (upload flow)
  Database-Architecture-Design-Documentation.md §13–14 (schema)
"""
from __future__ import annotations

import hashlib
import os
from enum import Enum


# ── Enumerations ──────────────────────────────────────────────────────────────

class DocumentType(str, Enum):
    """Allowed document_type values — enforced by DB CHECK constraint."""
    POLICY = "policy"
    PROCEDURE = "procedure"
    SOP = "sop"
    CONTRACT = "contract"
    TECHNICAL = "technical"
    REGULATORY = "regulatory"
    HR = "hr"
    MARKETING = "marketing"
    OTHER = "other"


class DocumentStatus(str, Enum):
    """Logical document lifecycle (documents.status)."""
    ACTIVE = "active"
    ARCHIVED = "archived"


class AccessLevel(str, Enum):
    """Document access scope (documents.access_level)."""
    ORGANIZATION = "organization"  # visible to all org members
    RESTRICTED = "restricted"      # further scoped via document_permissions
    PRIVATE = "private"            # owner only


class VersionStatus(str, Enum):
    """Async processing pipeline status (document_versions.status)."""
    UPLOADED = "UPLOADED"
    PROCESSING = "PROCESSING"
    EXTRACTING = "EXTRACTING"
    OCR = "OCR"
    CHUNKING = "CHUNKING"
    EMBEDDING = "EMBEDDING"
    INDEXING = "INDEXING"
    READY = "READY"
    FAILED = "FAILED"


# ── File-type validation ───────────────────────────────────────────────────────

# Allow-list of accepted MIME types mapped to their canonical extensions.
# Only these file types are accepted by the upload endpoint.
ALLOWED_MIME_TYPES: dict[str, str] = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/msword": "doc",
    "text/plain": "txt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
}

# Reverse map: extension → canonical MIME type
ALLOWED_EXTENSIONS: frozenset[str] = frozenset(ALLOWED_MIME_TYPES.values())

# Maximum file size in bytes (100 MB default — overrideable per org in Phase 16)
DEFAULT_MAX_FILE_SIZE_BYTES: int = 100 * 1024 * 1024  # 100 MB


class FileValidationError(ValueError):
    """Raised when a file fails the upload validation pipeline."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def get_extension(filename: str) -> str:
    """Return the lowercased extension (without dot) of a filename."""
    _, ext = os.path.splitext(filename)
    return ext.lstrip(".").lower()


def validate_file_type(
    filename: str,
    declared_content_type: str,
    first_bytes: bytes,
) -> str:
    """Validate file type against the allow-list using three independent checks.

    Args:
        filename: Original client filename.
        declared_content_type: Content-Type header from the multipart upload.
        first_bytes: The first 1024+ bytes of the file for magic-byte detection.

    Returns:
        The canonical MIME type string.

    Raises:
        FileValidationError: if the file type is not allowed or checks are inconsistent.

    Three checks (all must pass):
    1. File extension is in the allow-list.
    2. Declared Content-Type is in the allow-list (strip parameters like ;charset=...).
    3. Magic-byte sniff (via `filetype`) matches an allowed type.

    Check 3 is the authoritative guard — the client controls #1 and #2,
    but cannot forge the first bytes of the actual file content.
    """
    # ── Extension check ───────────────────────────────────────────────────────
    ext = get_extension(filename)
    if not ext or ext not in ALLOWED_EXTENSIONS:
        raise FileValidationError(
            "INVALID_FILE_TYPE",
            f"File extension '.{ext}' is not allowed. "
            f"Allowed extensions: {', '.join(sorted(ALLOWED_EXTENSIONS))}.",
        )

    # ── Declared Content-Type check ───────────────────────────────────────────
    declared_mime = declared_content_type.split(";")[0].strip().lower()
    if declared_mime not in ALLOWED_MIME_TYPES:
        raise FileValidationError(
            "INVALID_FILE_TYPE",
            f"Content-Type '{declared_mime}' is not allowed. "
            f"Allowed types: {', '.join(sorted(ALLOWED_MIME_TYPES))}.",
        )

    # ── Magic-byte check (authoritative) ─────────────────────────────────────
    try:
        import filetype as ft
        detected = ft.guess(first_bytes)
    except ImportError:
        # If filetype is not installed, skip magic-byte check (dev fallback only)
        detected = None

    if detected is not None:
        detected_mime = detected.mime
        if detected_mime not in ALLOWED_MIME_TYPES:
            raise FileValidationError(
                "INVALID_FILE_TYPE",
                f"File content does not match an allowed type "
                f"(detected: '{detected_mime}'). "
                f"Ensure you are uploading a PDF, DOCX, or other supported document.",
            )

    # Return the canonical MIME from the allow-list, preferring the magic-byte result
    if detected is not None and detected.mime in ALLOWED_MIME_TYPES:
        return detected.mime
    return declared_mime


def validate_file_size(size_bytes: int, max_bytes: int = DEFAULT_MAX_FILE_SIZE_BYTES) -> None:
    """Raise FileValidationError if size_bytes exceeds max_bytes."""
    if size_bytes > max_bytes:
        max_mb = max_bytes / (1024 * 1024)
        actual_mb = size_bytes / (1024 * 1024)
        raise FileValidationError(
            "FILE_TOO_LARGE",
            f"File size {actual_mb:.1f} MB exceeds the {max_mb:.0f} MB limit.",
        )


# ── Hashing ───────────────────────────────────────────────────────────────────

def compute_sha256(data: bytes) -> str:
    """Compute the SHA-256 hex digest of a byte string."""
    return hashlib.sha256(data).hexdigest()


# ── Storage key ───────────────────────────────────────────────────────────────

def compute_storage_key(
    org_id: str,
    document_id: str,
    version_id: str,
    extension: str,
) -> str:
    """Compute the deterministic object-storage key for a document version.

    Format: organizations/{org_id}/documents/{doc_id}/versions/{ver_id}/original.{ext}

    Properties:
    - Org-namespaced (defense-in-depth — actual access control is application-layer)
    - Deterministic from known IDs (allows orphan detection / reconciliation)
    - extension is the lowercase file extension WITHOUT the dot

    See Backend-Architecture-Documentation.md §25.
    """
    ext = extension.lstrip(".")
    return f"organizations/{org_id}/documents/{document_id}/versions/{version_id}/original.{ext}"
