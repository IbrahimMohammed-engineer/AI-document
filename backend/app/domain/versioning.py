"""
Document versioning domain logic — pure, unit-testable, no I/O.

Contains:
  - resolve_current_version(): picks the version that is currently effective
    (or effective as of a given date).  Phase 12 fixes the broken no-``as_of``
    branch — it now treats "current" identically to ``as_of=today`` instead of
    looking for a non-existent ``is_current`` flag (§8.4).
  - validate_effective_window(): rejects invalid date windows with a 422 (§8.5).
  - classify_version_state(): derives the CURRENT/SUPERSEDED/SCHEDULED label
    without storing it (Backend §47 deliberate non-storage — §8.6).
  - VersionScope dataclass: encapsulates the scope parameters passed
    into permission-aware retrieval (all, specific documents, specific
    collections, optional point-in-time).

Business rules for resolve_current_version (write verbatim in tests too):
  1. A version is a CANDIDATE for "current as of date D" iff
     status == READY AND
     (effective_date IS NULL OR effective_date <= D) AND
     (expiration_date IS NULL OR expiration_date > D).
  2. Among candidates, the one with the LATEST effective_date wins
     (NULL effective_date ranks lowest — an undated version never beats a
     dated one).
  3. Ties (identical effective_date, including two NULLs) break by
     latest created_at.
  4. No candidate → returns None (not an error).
  5. Multiple READY versions with overlapping or missing effective dates
     can coexist — this function still picks exactly one deterministically.
  6. status != READY (any other status, including FAILED) is NEVER a
     candidate regardless of effective_date.

These are pure domain functions — callers (AuthorizationService, retriever)
use them with real ORM objects; tests use lightweight stubs.

See Backend-Architecture-Documentation.md §16 (Document Versioning),
§30 (Metadata Filtering — temporal scope), §47 (state classification).
PHASE-12-IMPLEMENTATION-PLAN.md §8.4–§8.6.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

from app.core.exceptions import ValidationError


# ── Internal helpers ──────────────────────────────────────────────────────────

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


# ── Version resolution ────────────────────────────────────────────────────────

def resolve_current_version(versions: list, as_of: date | datetime | None = None) -> object | None:
    """Return the current (or point-in-time effective) version from a list.

    Phase 12 fix (§8.4): the no-``as_of`` path now correctly behaves as
    ``as_of=today``, applying the same effective/expiration-window filtering
    the ``as_of`` branch already implements.  The old ``is_current``-flag
    lookup (which always returned nothing because no such column exists) has
    been removed.

    Selection rules (see module docstring for the full set):
      - Without ``as_of``: equivalent to ``as_of=date.today()``.
      - With ``as_of``:    candidates are versions whose effective period
        covers the given date; latest ``effective_date`` wins, tie-broken
        by ``created_at``.
      - Empty list or no candidates → None.

    This is a pure function: it never queries the database.  It operates on
    whatever list the caller provides, making it trivially unit-testable and
    safe to call from any layer.

    Args:
        versions: Any iterable of objects with ``status`` (str),
                  ``created_at`` (datetime) and optionally
                  ``effective_date`` / ``expiration_date`` attributes.
                  Empty list is explicitly supported.
        as_of:    Optional point-in-time for temporal scope resolution.
                  ``date`` or ``datetime``; ``None`` means "as of today".

    Returns:
        The chosen version object, or None when nothing matches.
    """
    if not versions:
        return None

    # Unified path: treat "current" as "as_of = today" (§8.4 fix)
    as_of_date: date
    if as_of is not None:
        parsed = _to_date(as_of)
        # Unparseable as_of — resolve "current" rather than silently
        # returning an empty set for every document (backward-compatible).
        as_of_date = parsed if parsed is not None else date.max
    else:
        as_of_date = date.today()

    candidates = []
    for v in versions:
        # Rule 6: only READY versions are candidates
        status = getattr(v, "status", None)
        if status != "READY":
            continue
        eff = _to_date(getattr(v, "effective_date", None))
        exp = _to_date(getattr(v, "expiration_date", None))
        if eff is not None and eff > as_of_date:
            continue  # not yet effective at the asked time
        if exp is not None and exp <= as_of_date:
            continue  # already expired at the asked time
        candidates.append(v)

    if not candidates:
        return None

    # Rule 2: latest effective_date wins; NULL ranks lowest.
    # Rule 3: tie-break by latest created_at.
    return max(
        candidates,
        key=lambda v: (
            _to_date(getattr(v, "effective_date", None)) or date.min,
            _created_at_of(v),
        ),
    )


# ── Effective-date validation ──────────────────────────────────────────────────

def validate_effective_window(
    effective_date: date | None,
    expiration_date: date | None,
) -> None:
    """Raise ValidationError (422) if expiration_date precedes effective_date.

    Called by DocumentService before persisting any upload / new-version
    that carries date window fields.  Malformed dates (which the service
    already rejects at the form-parse level) are not the concern here —
    this validates the *relative ordering* of valid dates.

    Args:
        effective_date:   parsed Date or None.
        expiration_date:  parsed Date or None.

    Raises:
        ValidationError: if expiration_date < effective_date.
    """
    if (
        effective_date is not None
        and expiration_date is not None
        and expiration_date < effective_date
    ):
        raise ValidationError(
            "expiration_date must be on or after effective_date.",
            field="expiration_date",
        )


# ── Version-state classification (deliberate non-storage — Backend §47) ───────

def classify_version_state(
    version: object,
    all_versions: list,
    today: date | None = None,
) -> Literal["CURRENT", "SUPERSEDED", "SCHEDULED"]:
    """Derive the business-facing state label for a single version.

    This label is deliberately NOT stored (Backend §47: «storing it as a
    column would require a DB trigger and is a classic source of data-integrity
    bugs»).  It is computed on-demand from the current date and the full version
    list for the document.

    States:
      CURRENT    — this version is the one resolve_current_version() would pick
                   today (it is "what the org is working from now").
      SUPERSEDED — READY but not current (a newer or dated version took over).
      SCHEDULED  — not yet in effect (effective_date is in the future, or the
                   version is not yet READY — still processing).

    Args:
        version:      The version whose state is being classified.
        all_versions: All versions for the same document (used to determine
                      which one resolve_current_version() picks).
        today:        Override for "today" (unit-test seam).  None = date.today().

    Returns:
        One of "CURRENT", "SUPERSEDED", "SCHEDULED".
    """
    today_date = today or date.today()
    status = getattr(version, "status", None)

    # Non-READY versions: SCHEDULED if future-dated, else SUPERSEDED
    if status != "READY":
        eff = _to_date(getattr(version, "effective_date", None))
        if eff is not None and eff > today_date:
            return "SCHEDULED"
        return "SUPERSEDED"

    # Among READY versions, check if this one is the chosen current
    ready_versions = [v for v in all_versions if getattr(v, "status", None) == "READY"]
    current = resolve_current_version(ready_versions, as_of=today_date)
    if current is not None and getattr(current, "id", None) == getattr(version, "id", None):
        return "CURRENT"

    # READY but not current — check if it's future-effective (SCHEDULED)
    eff = _to_date(getattr(version, "effective_date", None))
    if eff is not None and eff > today_date:
        return "SCHEDULED"

    return "SUPERSEDED"


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
