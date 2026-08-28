"""
Unit tests for job/version state machines and retry policy (Phase 4).

Pure-domain tests — no Docker, no database.
"""
from __future__ import annotations

import pytest

from app.domain.documents import VersionStatus
from app.domain.state_machines import (
    BACKOFF_BASE_SECONDS,
    BACKOFF_MAX_SECONDS,
    InvalidJobTransitionError,
    InvalidVersionTransitionError,
    JobStatus,
    JobType,
    assert_job_transition,
    assert_version_transition,
    get_backoff_seconds,
    get_max_attempts,
    is_valid_job_transition,
    is_valid_version_transition,
)


# ─── Job status state machine ─────────────────────────────────────────────────

@pytest.mark.unit
class TestJobTransitions:
    def test_pending_to_processing_is_valid(self):
        assert is_valid_job_transition(JobStatus.PENDING, JobStatus.PROCESSING)

    def test_processing_to_completed_is_valid(self):
        assert is_valid_job_transition(JobStatus.PROCESSING, JobStatus.COMPLETED)

    def test_processing_to_retrying_is_valid(self):
        assert is_valid_job_transition(JobStatus.PROCESSING, JobStatus.RETRYING)

    def test_processing_to_failed_is_valid(self):
        assert is_valid_job_transition(JobStatus.PROCESSING, JobStatus.FAILED)

    def test_retrying_to_processing_is_valid(self):
        assert is_valid_job_transition(JobStatus.RETRYING, JobStatus.PROCESSING)

    def test_pending_to_completed_is_invalid(self):
        with pytest.raises(InvalidJobTransitionError):
            assert_job_transition(JobStatus.PENDING, JobStatus.COMPLETED)

    def test_pending_to_retrying_is_invalid(self):
        with pytest.raises(InvalidJobTransitionError):
            assert_job_transition(JobStatus.PENDING, JobStatus.RETRYING)

    def test_retrying_to_completed_is_invalid(self):
        assert not is_valid_job_transition(JobStatus.RETRYING, JobStatus.COMPLETED)

    def test_retrying_to_failed_is_invalid(self):
        # Exhaustion happens from PROCESSING, never from RETRYING (Backend §47)
        assert not is_valid_job_transition(JobStatus.RETRYING, JobStatus.FAILED)

    def test_terminal_states_have_no_outgoing_transitions(self):
        for terminal in (JobStatus.COMPLETED, JobStatus.FAILED):
            for target in JobStatus:
                assert not is_valid_job_transition(terminal, target)

    def test_error_message_names_the_transition(self):
        with pytest.raises(InvalidJobTransitionError, match="PENDING.*COMPLETED"):
            assert_job_transition(JobStatus.PENDING, JobStatus.COMPLETED)


# ─── Version status state machine ─────────────────────────────────────────────

@pytest.mark.unit
class TestVersionTransitions:
    def test_uploaded_to_processing_is_valid(self):
        assert is_valid_version_transition(VersionStatus.UPLOADED, VersionStatus.PROCESSING)

    def test_full_forward_walk_is_valid(self):
        pipeline = [
            VersionStatus.UPLOADED, VersionStatus.PROCESSING, VersionStatus.EXTRACTING,
            VersionStatus.OCR, VersionStatus.CHUNKING, VersionStatus.EMBEDDING,
            VersionStatus.INDEXING, VersionStatus.READY,
        ]
        for current, target in zip(pipeline, pipeline[1:]):
            assert is_valid_version_transition(current, target), f"{current} → {target}"

    def test_failed_from_any_non_terminal_state(self):
        for status in VersionStatus:
            if status in (VersionStatus.READY, VersionStatus.FAILED):
                continue  # READY terminal; FAILED → FAILED is not a transition
            assert is_valid_version_transition(status, VersionStatus.FAILED)

    def test_failed_to_processing_is_the_explicit_retry(self):
        assert is_valid_version_transition(VersionStatus.FAILED, VersionStatus.PROCESSING)

    def test_ready_is_terminal(self):
        for target in VersionStatus:
            assert not is_valid_version_transition(VersionStatus.READY, target)

    def test_backwards_transition_is_invalid(self):
        with pytest.raises(InvalidVersionTransitionError):
            assert_version_transition(VersionStatus.EXTRACTING, VersionStatus.UPLOADED)

    def test_failed_to_failed_is_invalid(self):
        with pytest.raises(InvalidVersionTransitionError):
            assert_version_transition(VersionStatus.FAILED, VersionStatus.FAILED)


# ─── Retry policy + backoff ───────────────────────────────────────────────────

@pytest.mark.unit
class TestRetryPolicy:
    def test_deterministic_stages_get_single_attempt(self):
        assert get_max_attempts(JobType.CHUNKING) == 1
        assert get_max_attempts(JobType.INDEXING) == 1

    def test_provider_dependent_stages_get_retries(self):
        assert get_max_attempts(JobType.OCR) >= 3
        assert get_max_attempts(JobType.EMBEDDING) >= 3

    def test_extraction_has_a_retry_budget(self):
        assert get_max_attempts(JobType.EXTRACTION) >= 1

    def test_backoff_is_exponential(self):
        assert get_backoff_seconds(1) == BACKOFF_BASE_SECONDS
        assert get_backoff_seconds(2) == BACKOFF_BASE_SECONDS * 2
        assert get_backoff_seconds(3) == BACKOFF_BASE_SECONDS * 4

    def test_backoff_is_capped(self):
        assert get_backoff_seconds(50) == BACKOFF_MAX_SECONDS

    def test_backoff_handles_out_of_range_input(self):
        assert get_backoff_seconds(0) == BACKOFF_BASE_SECONDS
