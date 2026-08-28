"""
Unit tests for app.domain.versioning — pure, no I/O.

Tests:
  - resolve_current_version: empty list, single item, flagged item,
    multiple flagged (picks latest), no flag (falls back to latest by
    created_at), and the Phase 8 temporal ``as_of`` resolution
    (Backend §30 — effective-version-at-point-in-time)
  - VersionScope: constructors, frozen dataclass behaviour
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

import pytest

from app.domain.versioning import VersionScope, resolve_current_version


# ── Stub version object ───────────────────────────────────────────────────────

@dataclass
class FakeVersion:
    id: str
    is_current: bool
    created_at: datetime
    status: str = "READY"


@dataclass
class DatedVersion:
    """Version with effective/expiration dates (Phase 8 temporal tests)."""

    id: str
    created_at: datetime
    effective_date: date | None = None
    expiration_date: date | None = None
    is_current: bool = False
    status: str = "READY"


def make_version(id: str, is_current: bool, days_ago: int = 0, status: str = "READY") -> FakeVersion:
    return FakeVersion(
        id=id,
        is_current=is_current,
        created_at=datetime(2024, 1, 1 + days_ago, tzinfo=timezone.utc),
        status=status,
    )


# ── resolve_current_version ───────────────────────────────────────────────────

class TestResolveCurrentVersion:

    def test_empty_list_returns_none(self):
        assert resolve_current_version([]) is None

    def test_single_item_always_returned(self):
        v = make_version("v1", is_current=False)
        result = resolve_current_version([v])
        assert result is v

    def test_flagged_item_preferred_over_newer(self):
        """is_current=True wins even when there's a more recent version."""
        old_current = make_version("v1", is_current=True, days_ago=0)
        new_not_current = make_version("v2", is_current=False, days_ago=1)
        result = resolve_current_version([old_current, new_not_current])
        assert result is old_current

    def test_no_flag_picks_latest_by_created_at(self):
        """If no is_current flag is set, falls back to the most recent."""
        v1 = make_version("v1", is_current=False, days_ago=0)
        v2 = make_version("v2", is_current=False, days_ago=1)
        v3 = make_version("v3", is_current=False, days_ago=2)
        result = resolve_current_version([v1, v2, v3])
        assert result is v3  # most days_ago = earliest date... wait, days_ago 2 is oldest

    def test_no_flag_picks_latest_date(self):
        """Confirm 'latest' means latest created_at (most recent calendar date)."""
        v_old = FakeVersion("v_old", is_current=False,
                            created_at=datetime(2024, 1, 1, tzinfo=timezone.utc))
        v_new = FakeVersion("v_new", is_current=False,
                            created_at=datetime(2024, 6, 1, tzinfo=timezone.utc))
        result = resolve_current_version([v_old, v_new])
        assert result is v_new

    def test_multiple_flagged_picks_most_recent(self):
        """Defensive: if multiple rows have is_current=True, pick the latest."""
        v_old = FakeVersion("v_old", is_current=True,
                            created_at=datetime(2024, 1, 1, tzinfo=timezone.utc))
        v_new = FakeVersion("v_new", is_current=True,
                            created_at=datetime(2024, 6, 1, tzinfo=timezone.utc))
        result = resolve_current_version([v_old, v_new])
        assert result is v_new

    def test_mixed_flags_returns_flagged(self):
        """Version with is_current=True wins over a later version without the flag."""
        flagged = FakeVersion("flagged", is_current=True,
                              created_at=datetime(2024, 1, 1, tzinfo=timezone.utc))
        unflagged_newer = FakeVersion("newer", is_current=False,
                                     created_at=datetime(2024, 12, 1, tzinfo=timezone.utc))
        result = resolve_current_version([flagged, unflagged_newer])
        assert result is flagged

    def test_order_independent(self):
        """Result should not depend on list ordering."""
        v1 = make_version("v1", is_current=True, days_ago=0)
        v2 = make_version("v2", is_current=False, days_ago=1)

        assert resolve_current_version([v1, v2]) is v1
        assert resolve_current_version([v2, v1]) is v1


# ── Phase 8: temporal (as_of) version resolution ─────────────────────────────

class TestResolveVersionAsOf:
    """Backend §30: "the version effective at time T" resolves through the
    SAME function as "current", parameterized by a point in time."""

    def test_version_effective_at_as_of_wins(self):
        v2024 = DatedVersion("v2024", created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
                             effective_date=date(2024, 1, 1))
        v2026 = DatedVersion("v2026", created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                             effective_date=date(2026, 1, 1))
        result = resolve_current_version([v2024, v2026], as_of=date(2025, 6, 15))
        assert result is v2024

    def test_not_yet_effective_version_excluded(self):
        v2026 = DatedVersion("v2026", created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                             effective_date=date(2026, 1, 1))
        result = resolve_current_version([v2026], as_of=date(2025, 6, 15))
        assert result is None  # "didn't exist then" — excluded, not an error

    def test_expired_version_excluded(self):
        v = DatedVersion("v", created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
                         effective_date=date(2020, 1, 1),
                         expiration_date=date(2024, 12, 31))
        assert resolve_current_version([v], as_of=date(2025, 6, 15)) is None
        assert resolve_current_version([v], as_of=date(2023, 6, 15)) is v

    def test_latest_effective_date_wins(self):
        v1 = DatedVersion("v1", created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
                          effective_date=date(2024, 1, 1))
        v2 = DatedVersion("v2", created_at=datetime(2024, 6, 1, tzinfo=timezone.utc),
                          effective_date=date(2024, 6, 1))
        result = resolve_current_version([v1, v2], as_of=date(2025, 1, 1))
        assert result is v2

    def test_effective_on_boundary_day_inclusive(self):
        v = DatedVersion("v", created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
                         effective_date=date(2024, 3, 1))
        assert resolve_current_version([v], as_of=date(2024, 3, 1)) is v

    def test_expiration_boundary_exclusive(self):
        v = DatedVersion("v", created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
                         effective_date=date(2024, 1, 1),
                         expiration_date=date(2024, 12, 31))
        assert resolve_current_version([v], as_of=date(2024, 12, 31)) is None
        assert resolve_current_version([v], as_of=date(2024, 12, 30)) is v

    def test_undated_version_never_beats_dated_one(self):
        undated = DatedVersion("undated", created_at=datetime(2024, 1, 1, tzinfo=timezone.utc))
        dated = DatedVersion("dated", created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
                             effective_date=date(2020, 1, 1))
        result = resolve_current_version([undated, dated], as_of=date(2025, 1, 1))
        assert result is dated

    def test_only_undated_version_still_matches(self):
        undated = DatedVersion("undated", created_at=datetime(2024, 1, 1, tzinfo=timezone.utc))
        result = resolve_current_version([undated], as_of=date(2025, 1, 1))
        assert result is undated

    def test_iso_string_as_of_accepted(self):
        v = DatedVersion("v", created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
                         effective_date=date(2024, 1, 1))
        assert resolve_current_version([v], as_of="2025-06-15") is v

    def test_unparseable_as_of_falls_back_to_current(self):
        """A malformed as_of must not silently empty the candidate set."""
        v = DatedVersion("v", created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
                         effective_date=date(2024, 1, 1))
        assert resolve_current_version([v], as_of="not-a-date") is v

    def test_none_as_of_uses_current_logic(self):
        flagged = DatedVersion("flagged", created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
                               is_current=True)
        newer = DatedVersion("newer", created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        result = resolve_current_version([flagged, newer], as_of=None)
        assert result is flagged

    def test_empty_list_with_as_of(self):
        assert resolve_current_version([], as_of=date(2025, 1, 1)) is None


# ── VersionScope ──────────────────────────────────────────────────────────────

class TestVersionScope:

    def test_all_documents_factory(self):
        scope = VersionScope.all_documents()
        assert scope.kind == "all"
        assert scope.document_ids == ()
        assert scope.collection_ids == ()

    def test_for_documents_factory(self):
        scope = VersionScope.for_documents(["id-1", "id-2"])
        assert scope.kind == "documents"
        assert scope.document_ids == ("id-1", "id-2")

    def test_for_collections_factory(self):
        scope = VersionScope.for_collections(["col-1"])
        assert scope.kind == "collections"
        assert scope.collection_ids == ("col-1",)

    def test_frozen(self):
        """VersionScope is immutable (frozen=True)."""
        scope = VersionScope.all_documents()
        with pytest.raises((AttributeError, TypeError)):
            scope.kind = "documents"  # type: ignore[misc]

    def test_default_as_of_is_none(self):
        scope = VersionScope.all_documents()
        assert scope.as_of is None

    def test_equality(self):
        s1 = VersionScope.for_documents(["id-1"])
        s2 = VersionScope.for_documents(["id-1"])
        assert s1 == s2

    def test_hashable(self):
        """Frozen dataclass should be hashable (usable as dict keys)."""
        scope = VersionScope.all_documents()
        d = {scope: "value"}
        assert d[scope] == "value"
