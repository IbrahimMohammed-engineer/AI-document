"""
Document versioning domain logic — pure, unit-testable, no I/O.

Contains:
  - resolve_current_version(): picks the active (is_current) version
    from a list, or falls back to the latest by created_at.  The optional
    ``as_of`` parameter reuses the SAME function for temporal questions
    (Backend §30): "the version effective at time T" is "current version"
    parameterized by a point in time, never a separate historical path.
  - VersionScope dataclass: encapsulates the scope parameters passed
    into permission-aware retrieval (all, specific documents, specific
    collections, optional point-in-time).

These are pure domain functions — callers (AuthorizationService, retriever)
use them with real ORM objects; tests use lightweight stubs.

See Backend-Architecture-Documentation.md §16 (Document Versioning),
§30 (Metadata Filtering — temporal scope).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal


# ── Version resolution ────────────────────────────────────────────────────────

def _to_date(value: object) -> date | None:
    """Coerce a version's effective/expiration attribute to a date.

    Accepts ``date``/``datetime`` (ORM attribute types) and ISO-8601
    strings; returns None for NULL columns.  Non-parseable values are
    treated as NULL defensively rather than raising — a malformed date
    must not take down a search request.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _created_at_of(version: object) -> datetime:
    """Read a version's ``created_at`` with a safe epoch fallback.

    Defensive against stub objects in tests and NULL columns — sorting
    must never crash on a missing attribute.
    """
    value = getattr(version, "created_at", None)
    if isinstance(value, datetime):
        return value
    return datetime.min


def resolve_current_version(versions: list, as_of: date | datetime | None = None) -> object | None:
    """Return the current (or point-in-time effective) version from a list.

    Selection order without ``as_of`` (Backend §16):
      1. The row with ``is_current = True`` (authoritative flag).
      2. If no row is flagged, fall back to the most recent by ``created_at``
         (defensive — the flag should always be set by the ingestion pipeline).
      3. Empty list → None.

    With ``as_of`` set (Backend §30 — temporal/effective-date scope):
      1. Candidates are versions whose effective period covers ``as_of``:
         (effective_date IS NULL OR effective_date <= as_of) AND
         (expiration_date IS NULL OR expiration_date > as_of).
      2. Among candidates, the latest ``effective_date`` wins (NULL
         effective_date ranks lowest — an undated version never beats a
         dated one), tie-broken by ``created_at``.
      3. No candidate covers ``as_of`` → None: the document is excluded
         from the query's candidate set ("this document didn't exist /
         wasn't effective then") — NOT an error (Backend §30 step 4).

    This is a pure function: it never queries the database.  It operates on
    whatever list the caller provides, making it trivially unit-testable and
    safe to call from any layer.

    Args:
        versions: Any iterable of objects with ``is_current`` (bool),
                  ``created_at`` (datetime) and optionally ``effective_date`` /
                  ``expiration_date`` attributes.  Empty list is explicitly
                  supported.
        as_of:    Optional point-in-time for temporal scope resolution.
                  ``date`` or ``datetime``; ``None`` means "current".

    Returns:
        The chosen version object, or None when nothing matches.
    """
    if not versions:
        return None

    if as_of is not None:
        as_of_date = _to_date(as_of)
        if as_of_date is None:
            # Unparseable as_of — resolve "current" rather than silently
            # returning an empty set for every document.
            as_of_date = date.max

        candidates = []
        for v in versions:
            eff = _to_date(getattr(v, "effective_date", None))
            exp = _to_date(getattr(v, "expiration_date", None))
            if eff is not None and eff > as_of_date:
                continue  # not yet effective at the asked time
            if exp is not None and exp <= as_of_date:
                continue  # already expired at the asked time
            candidates.append(v)

        if not candidates:
            return None

        # Latest effective_date wins; NULL effective_date ranks lowest;
        # tie-break by created_at (fresh supersede older duplicates).
        return max(
            candidates,
            key=lambda v: (
                _to_date(getattr(v, "effective_date", None)) or date.min,
                _created_at_of(v),
            ),
        )

    # Prefer the explicitly flagged current version
    flagged = [v for v in versions if getattr(v, "is_current", False)]
    if flagged:
        # In a correctly maintained corpus there is exactly one; pick the
        # most recent if there are multiple (defensive).
        return max(flagged, key=_created_at_of)

    # Fallback: latest by creation time (covers any state where the flag
    # was not set — should not happen in production, logged upstream)
    return max(versions, key=_created_at_of)


# ── Search scope ──────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class VersionScope:
    """Describes which documents/versions are in scope for a retrieval call.

    Three scope types (Backend §29):
      - ``"all"``:           everything the user can read in their org.
      - ``"documents"``:     a specific subset of document IDs.
      - ``"collections"``:   all documents in a set of collection IDs.

    ``as_of`` is an optional ISO-8601 datetime string.  When set,
    ``resolve_current_version`` is called with a point-in-time filter so
    temporal questions retrieve the effective version at that moment
    (Phase 8 — ignored in Phase 7, documented here for forward-compat).
    """

    kind: Literal["all", "documents", "collections"] = "all"
    document_ids: tuple[str, ...] = field(default_factory=tuple)
    collection_ids: tuple[str, ...] = field(default_factory=tuple)
    as_of: str | None = None  # ISO-8601 datetime string; None = current

    # Convenience constructors ─────────────────────────────────────────────────

    @classmethod
    def all_documents(cls) -> "VersionScope":
        """Scope: every document the user is permitted to read."""
        return cls(kind="all")

    @classmethod
    def for_documents(cls, document_ids: list[str]) -> "VersionScope":
        """Scope: only the listed document IDs."""
        return cls(kind="documents", document_ids=tuple(document_ids))

    @classmethod
    def for_collections(cls, collection_ids: list[str]) -> "VersionScope":
        """Scope: all documents inside the listed collection IDs."""
        return cls(kind="collections", collection_ids=tuple(collection_ids))
