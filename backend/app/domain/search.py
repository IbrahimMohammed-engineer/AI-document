"""
Search filter domain model — the metadata filter dimensions for retrieval.

Phase 8 (Backend §30): ``document_type``, ``collection_id``, ``department``,
``owner_id``.  The temporal/effective-date scope is NOT here — it lives on
``VersionScope.as_of`` and resolves through ``resolve_current_version(as_of=…)``
(the same domain function as "current" resolution, parameterized).

All values are optional; ``None``/empty means "no filter on this dimension".
Filters are structural data passed down into the retrieval repository, where
they become additional WHERE clauses in the SAME SQL statement as the
mandatory permission predicates (never a post-retrieval filter — Backend
§29/§30: one retrieval query per search, predicates in the same query plan).
"""
from __future__ import annotations

from dataclasses import dataclass, field


# Allowed document_type values — mirrors the DB CHECK constraint
# ck_documents_document_type (migration 004 / models/document.py).
ALLOWED_DOCUMENT_TYPES: frozenset[str] = frozenset(
    {
        "policy",
        "procedure",
        "sop",
        "contract",
        "technical",
        "regulatory",
        "hr",
        "marketing",
        "other",
    }
)


@dataclass(frozen=True, slots=True)
class SearchFilters:
    """Metadata filter dimensions applied inside the retrieval SQL."""

    document_types: tuple[str, ...] = field(default_factory=tuple)
    collection_ids: tuple[str, ...] = field(default_factory=tuple)
    department: str | None = None
    owner_id: str | None = None

    @property
    def is_empty(self) -> bool:
        """True when no filter dimension is active."""
        return not (
            self.document_types
            or self.collection_ids
            or self.department
            or self.owner_id
        )

    @classmethod
    def empty(cls) -> "SearchFilters":
        """A filter set with every dimension disabled."""
        return cls()

    def sanitized(self) -> "SearchFilters":
        """Return a copy with unknown document_type values dropped.

        Unknown types can never match the DB CHECK constraint.  When a
        mix of known + unknown values is supplied, the unknown ones are
        dropped so a client typo cannot empty the result set alongside a
        valid type.  If ONLY unknown types were supplied they are KEPT —
        the query then legitimately matches nothing, rather than silently
        ignoring the user's filter entirely.
        """
        known = tuple(t for t in self.document_types if t in ALLOWED_DOCUMENT_TYPES)
        unknown = tuple(t for t in self.document_types if t not in ALLOWED_DOCUMENT_TYPES)
        if not unknown or not known:
            return self
        return SearchFilters(
            document_types=known,
            collection_ids=self.collection_ids,
            department=self.department,
            owner_id=self.owner_id,
        )
