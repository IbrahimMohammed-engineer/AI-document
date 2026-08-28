"""
State machines and job policies — pure, unit-testable business rules.

Contains:
  - JobType / JobStatus enums (processing_jobs)
  - Job-status transition validation (Backend §47):
        PENDING → PROCESSING → COMPLETED
                     │
                     ├──→ FAILED   (retries exhausted)
                     └──→ RETRYING → PROCESSING
  - DocumentVersion-status transition validation (Backend §47):
        strictly forward through the pipeline; FAILED reachable from any
        non-terminal state; FAILED → PROCESSING only via the explicit retry
        action; READY terminal.
  - Per-job-type retry policy and exponential backoff computation.

No I/O, no database access — pure functions and constants only.

See Backend-Architecture-Documentation.md §47 (State Machines), §49 (Idempotency),
and the Phase 4 roadmap (retry policy, RETRYING visibility).
"""
from __future__ import annotations

from enum import Enum

from app.domain.documents import VersionStatus


# ── Enumerations ──────────────────────────────────────────────────────────────

class JobType(str, Enum):
    """Values of processing_jobs.job_type (enforced by DB CHECK constraint).

    Phase 4 registers only EXTRACTION (trivial validate handler — Phase 5
    replaces it with real extraction). The other types exist so later phases
    add handlers, not migrations.
    """
    EXTRACTION = "EXTRACTION"
    OCR = "OCR"
    CHUNKING = "CHUNKING"
    EMBEDDING = "EMBEDDING"
    INDEXING = "INDEXING"
    COMPARISON = "COMPARISON"      # Phase 12
    SUMMARY = "SUMMARY"            # Phase 14
    CONFLICT_SCAN = "CONFLICT_SCAN"  # Phase 13
    PURGE = "PURGE"                # Phase 16/20 retention maintenance


class JobStatus(str, Enum):
    """Values of processing_jobs.status (enforced by DB CHECK constraint)."""
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    RETRYING = "RETRYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"

    @classmethod
    def active_states(cls) -> frozenset[JobStatus]:
        """States in which a job still has work or retries outstanding."""
        return frozenset({cls.PENDING, cls.PROCESSING, cls.RETRYING})


# ── Job-status state machine ──────────────────────────────────────────────────

_JOB_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.PENDING: frozenset({JobStatus.PROCESSING}),
    JobStatus.PROCESSING: frozenset(
        {JobStatus.COMPLETED, JobStatus.RETRYING, JobStatus.FAILED}
    ),
    JobStatus.RETRYING: frozenset({JobStatus.PROCESSING}),
    # Terminal states have no outgoing transitions
    JobStatus.COMPLETED: frozenset(),
    JobStatus.FAILED: frozenset(),
}


class InvalidJobTransitionError(ValueError):
    """Raised when a job-status transition violates the state machine."""

    def __init__(self, current: JobStatus, target: JobStatus) -> None:
        super().__init__(
            f"Invalid job status transition: {current.value} → {target.value}"
        )
        self.current = current
        self.target = target


def is_valid_job_transition(current: JobStatus, target: JobStatus) -> bool:
    """Return True if current → target is a valid job-status transition."""
    return target in _JOB_TRANSITIONS[current]


def assert_job_transition(current: JobStatus, target: JobStatus) -> None:
    """Raise InvalidJobTransitionError if current → target is not allowed.

    The state machine is enforced here — never "merely expected to hold".
    Re-terminal transitions (e.g. COMPLETED → anything) are programming
    errors and always raise.
    """
    if not is_valid_job_transition(current, target):
        raise InvalidJobTransitionError(current, target)


# ── DocumentVersion-status state machine ──────────────────────────────────────

# Pipeline order defines "forward" (Backend §47 — strictly forward; stages may
# be skipped only where semantically inapplicable, never replayed backwards).
_PIPELINE_ORDER: tuple[VersionStatus, ...] = (
    VersionStatus.UPLOADED,
    VersionStatus.PROCESSING,
    VersionStatus.EXTRACTING,
    VersionStatus.OCR,
    VersionStatus.CHUNKING,
    VersionStatus.EMBEDDING,
    VersionStatus.INDEXING,
    VersionStatus.READY,
)

_VERSION_TRANSITIONS: dict[VersionStatus, frozenset[VersionStatus]] = {
    status: frozenset(_PIPELINE_ORDER[i + 1:])  # everything ahead is forward
    for i, status in enumerate(_PIPELINE_ORDER)
}
# FAILED is reachable from any non-terminal state
for status in _PIPELINE_ORDER:
    _VERSION_TRANSITIONS[status] = _VERSION_TRANSITIONS[status] | {
        VersionStatus.FAILED
    }
# FAILED → PROCESSING is the explicit, deliberate retry action (never automatic)
_VERSION_TRANSITIONS[VersionStatus.FAILED] = frozenset({VersionStatus.PROCESSING})
# READY is terminal for the processing state machine
_VERSION_TRANSITIONS[VersionStatus.READY] = frozenset()


class InvalidVersionTransitionError(ValueError):
    """Raised when a version-status transition violates the state machine."""

    def __init__(self, current: VersionStatus, target: VersionStatus) -> None:
        super().__init__(
            f"Invalid version status transition: {current.value} → {target.value}"
        )
        self.current = current
        self.target = target


def is_valid_version_transition(current: VersionStatus, target: VersionStatus) -> bool:
    """Return True if current → target is a valid version-status transition."""
    return target in _VERSION_TRANSITIONS[current]


def assert_version_transition(current: VersionStatus, target: VersionStatus) -> None:
    """Raise InvalidVersionTransitionError if current → target is not allowed."""
    if not is_valid_version_transition(current, target):
        raise InvalidVersionTransitionError(current, target)


# ── Job execution error types ─────────────────────────────────────────────────

class DeterministicJobError(Exception):
    """A job failed for a reason that will not change on retry.

    Examples: corrupt file, missing storage object, tenancy mismatch.
    The worker marks the job FAILED immediately — no retry budget spent.
    """

    def __init__(self, message: str, *, code: str = "JOB_DETERMINISTIC_FAILURE") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class RetryableJobError(Exception):
    """A job failed transiently — a retry may succeed (provider timeout, 5xx).

    The worker applies the retry policy: RETRYING + backoff while the budget
    lasts, then terminal FAILED + dead-letter.
    """

    def __init__(self, message: str, *, code: str = "JOB_TRANSIENT_FAILURE") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


# ── Retry policy (per job type) and backoff ───────────────────────────────────

# Provider-dependent stages are prone to transient failure → larger budget.
# Deterministic internal stages fail for a reason that will not fix itself →
# a single attempt (Backend §23 retry guidance).
_JOB_RETRY_POLICY: dict[JobType, int] = {
    JobType.EXTRACTION: 3,   # parser/storage — usually deterministic, but
    #                         transient storage hiccups deserve a retry
    JobType.OCR: 3,          # external provider (Phase 5+)
    JobType.CHUNKING: 1,     # deterministic internal computation
    JobType.EMBEDDING: 3,    # external provider (Phase 7)
    JobType.INDEXING: 1,     # verification pass
    JobType.COMPARISON: 3,   # may call LLM providers (Phase 12)
    JobType.SUMMARY: 3,      # may call LLM providers (Phase 14)
    JobType.CONFLICT_SCAN: 2,
    JobType.PURGE: 3,        # touches object storage / DB teardown
}


def get_max_attempts(job_type: JobType) -> int:
    """Return the configured retry budget for a job type."""
    return _JOB_RETRY_POLICY[job_type]


# Exponential backoff: base * 2^(attempt-1), capped.
BACKOFF_BASE_SECONDS = 5
BACKOFF_MAX_SECONDS = 300


def get_backoff_seconds(attempt: int) -> int:
    """Return the delay before retry `attempt + 1` (exponential, capped).

    attempt is the 1-based number of the attempt that just failed:
      attempt=1 → 5s, attempt=2 → 10s, attempt=3 → 20s … capped at 300s.
    """
    if attempt < 1:
        attempt = 1
    delay = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
    return min(delay, BACKOFF_MAX_SECONDS)
