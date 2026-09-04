"""
Unit tests for app.domain.versioning — pure, no I/O.

Tests:
  - resolve_current_version: the §8.4 business rules — candidate =
    READY + effective-window covers D; latest effective_date wins (NULL
    ranks lowest); ties break by latest created_at; no candidate → None;
    non-READY never selected.  "Current" == "as_of=today" (Phase 12 fix —
    the old is_current-flag lookup never existed in the schema).
  - validate_effective_window (§8.5 — 422 on inverted windows)
  - classify_version_state (§8.6 — CURRENT/SUPERSEDED/SCHEDULED)
  - VersionScope: constructors, frozen dataclass behaviour
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

import pytest

from app.core.exceptions import ValidationError
from app.domain.versioning import (
    VersionScope,
    classify_version_state,
    resolve_current_version,
    validate_effective_window,
)


# ── Stub version object ───────────────────────────────────────────────────────

@dataclass
class FakeVersion:
    id: str
    created_at: datetime
    status: str = "READY"


@dataclass
class DatedVersion:
    """Version with effective/expiration dates (temporal tests)."""

    id: str
    created_at: datetime
    effective_date: date | None = None
    expiration_date: date | None = None
    status: str = "READY"


def make_version(id: str, days_ago: int = 0, status: str = "READY") -> FakeVersion:
    return FakeVersion(
        id=id,
        created_at=datetime(2024, 1, 1 + days_ago, tzinfo=timezone.utc),
        status=status,
    )


# ── resolve_current_version ───────────────────────────────────────────────────

class TestResolveCurrentVersion:

    def test_empty_list_returns_none(self):
        assert resolve_current_version([]) is None

    def test_single_item_always_returned(self):
        v = make_version("v1")
        result = resolve_current_version([v])
        assert result is v

    def test_latest_created_at_wins_for_undated_versions(self):
        """Rule 3: identical (NULL) effective dates tie-break by created_at."""
        v_old = FakeVersion("v_old", created_at=datetime(2024, 1, 1, tzinfo=timezone.utc))
        v_new = FakeVersion("v_new", created_at=datetime(2024, 6, 1, tzinfo=timezone.utc))
        result = resolve_current_version([v_old, v_new])
        assert result is v_new

    def test_no_flag_picks_latest_by_created_at(self):
        """Multiple undated READY versions → most recently created wins."""
        v1 = make_version("v1", days_ago=0)
        v2 = make_version("v2", days_ago=1)
        v3 = make_version("v3", days_ago=2)
        result = resolve_current_version([v1, v2, v3])
        # days_ago shifts the date LATER (2024-01-01 + days_ago)
        assert result is v3

    def test_order_independent(self):
        """Result should not depend on list ordering (§8.4 rule 3)."""
        v1 = make_version("v1", days_ago=0)
        v2 = make_version("v2", days_ago=1)

        assert resolve_current_version([v1, v2]) is v2
        assert resolve_current_version([v2, v1]) is v2

    def test_future_effective_version_never_current_today(self):
        """§8.4 fix: a future-dated (scheduled) READY version is NOT picked
        as current — the currently-effective one wins.  Uses far-future
        dates so the test is stable regardless of the real clock."""
        current = DatedVersion("current", created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
                               effective_date=date(2024, 1, 1))
        scheduled = DatedVersion("scheduled", created_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
                                 effective_date=date(2999, 1, 1))
        result = resolve_current_version([current, scheduled])
        assert result is current

    def test_only_future_dated_version_returns_none(self):
        """A document whose only version is scheduled has NO current version
        yet — None, not the scheduled one (§8.4 rule 4)."""
        scheduled = DatedVersion("scheduled", created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
                                 effective_date=date(2999, 1, 1))
        assert resolve_current_version([scheduled], as_of=date(2025, 6, 15)) is None

    def test_expired_version_excluded_from_current(self):
        expired = DatedVersion("expired", created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
                               effective_date=date(2020, 1, 1),
                               expiration_date=date(2024, 12, 31))
        assert resolve_current_version([expired], as_of=date(2025, 6, 15)) is None

    def test_non_ready_versions_never_selected(self):
        """Rule 6: any non-READY status is never a candidate, dates or not."""
        failed = DatedVersion("failed", created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
                              effective_date=date(2020, 1, 1), status="FAILED")
        processing = DatedVersion("processing", created_at=datetime(2024, 6, 1, tzinfo=timezone.utc),
                                  status="PROCESSING")
        assert resolve_current_version([failed, processing]) is None
        ready = DatedVersion("ready", created_at=datetime(2023, 1, 1, tzinfo=timezone.utc),
                             effective_date=date(2020, 1, 1))
        assert resolve_current_version([failed, processing, ready]) is ready

    def test_overlapping_windows_deterministic(self):
        """Rule 5: overlapping windows are not an error — the latest
        effective_date wins deterministically."""
        v1 = DatedVersion("v1", created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
                          effective_date=date(2024, 1, 1))
        v2 = DatedVersion("v2", created_at=datetime(2024, 6, 1, tzinfo=timezone.utc),
                          effective_date=date(2024, 1, 1))  # same effective_date
        result = resolve_current_version([v1, v2], as_of=date(2025, 1, 1))
        assert result is v2  # tie on effective_date → latest created_at

    def test_undated_never_beats_dated_on_created_at_alone(self):
        """Rule 2: an undated version ranks lowest even when newer."""
        undated = DatedVersion("undated", created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        dated = DatedVersion("dated", created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
                             effective_date=date(2020, 1, 1))
        assert resolve_current_version([undated, dated], as_of=date(2025, 1, 1)) is dated


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

    def test_none_as_of_equals_as_of_today(self):
        """§8.4: the default path IS the temporal path with D=today — one
        code path, no separate 'current' logic."""
        current = DatedVersion("current", created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
                               effective_date=date(2024, 1, 1))
        scheduled = DatedVersion("scheduled", created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                                 effective_date=date(2999, 1, 1))
        assert resolve_current_version([current, scheduled]) is \
            resolve_current_version([current, scheduled], as_of=date.today())

    def test_empty_list_with_as_of(self):
        assert resolve_current_version([], as_of=date(2025, 1, 1)) is None


# ── validate_effective_window (§8.5) ─────────────────────────────────────────

class TestValidateEffectiveWindow:

    def test_valid_window_passes(self):
        validate_effective_window(date(2025, 1, 1), date(2026, 1, 1))

    def test_both_none_passes(self):
        validate_effective_window(None, None)

    def test_effective_only_passes(self):
        validate_effective_window(date(2025, 1, 1), None)

    def test_expiration_only_passes(self):
        validate_effective_window(None, date(2026, 1, 1))

    def test_equal_dates_pass_boundary(self):
        """expiration == effective is ALLOWED (must be on-or-after)."""
        validate_effective_window(date(2025, 1, 1), date(2025, 1, 1))

    def test_inverted_window_raises_422(self):
        with pytest.raises(ValidationError) as exc:
            validate_effective_window(date(2026, 1, 1), date(2025, 1, 1))
        assert "expiration_date" in str(exc.value)


# ── classify_version_state (§8.6) ────────────────────────────────────────────

class TestClassifyVersionState:

    def _three_version_fixture(self, today: date):
        """v1: READY, past-effective → SUPERSEDED once v2 took over.
        v2: READY, currently effective → CURRENT.
        v3: READY, future effective → SCHEDULED."""
        v1 = DatedVersion("v1", created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
                          effective_date=date(2024, 1, 1))
        v2 = DatedVersion("v2", created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
                          effective_date=date(2025, 1, 1))
        v3 = DatedVersion("v3", created_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
                          effective_date=date(2999, 1, 1))
        return v1, v2, v3

    def test_three_version_fixture(self):
        today = date(2025, 6, 15)
        v1, v2, v3 = self._three_version_fixture(today)
        versions = [v1, v2, v3]
        assert classify_version_state(v1, versions, today=today) == "SUPERSEDED"
        assert classify_version_state(v2, versions, today=today) == "CURRENT"
        assert classify_version_state(v3, versions, today=today) == "SCHEDULED"

    def test_non_ready_future_dated_is_scheduled(self):
        today = date(2025, 6, 15)
        processing = DatedVersion("p", created_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
                                  effective_date=date(2998, 1, 1), status="PROCESSING")
        assert classify_version_state(processing, [processing], today=today) == "SCHEDULED"

    def test_non_ready_undated_is_superseded(self):
        today = date(2025, 6, 15)
        processing = DatedVersion("p", created_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
                                  status="PROCESSING")
        assert classify_version_state(processing, [processing], today=today) == "SUPERSEDED"

    def test_single_ready_version_is_current(self):
        today = date(2025, 6, 15)
        only = DatedVersion("only", created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
                            effective_date=date(2024, 1, 1))
        assert classify_version_state(only, [only], today=today) == "CURRENT"

    def test_failed_version_is_superseded(self):
        today = date(2025, 6, 15)
        ready = DatedVersion("ready", created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
                             effective_date=date(2024, 1, 1))
        failed = DatedVersion("failed", created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
                              status="FAILED")
        assert classify_version_state(failed, [ready, failed], today=today) == "SUPERSEDED"


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
