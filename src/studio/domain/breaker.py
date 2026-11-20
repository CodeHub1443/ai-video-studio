"""Crash-loop breaker and startup interruption marking (UD-7, design C.6 and C.7). Pure functions; no clock.

The breaker counts *interruptions that are not the engine's fault*: an attempt that had started and was ended by
a studio-process crash or a host restart (``recovery.counts_toward_breaker``). A user stop does not count, an
attempt that never started does not count, and engine-side failures consume the retry budget instead.

* An interrupt is counted by the time it was **interrupted** (``finished_at``), not the time it started: a
  long-running attempt that dies now belongs to the current window.
* ``K`` (interrupts) and ``T`` (window, seconds) are configuration owned by the caller; they are parameters here
  and have no default. While ``k`` is ``None`` the breaker fails closed: it behaves as if ``K`` were one, so a
  single counted interrupt pauses the queue; while ``window_s`` is ``None`` the window is unbounded (every
  counted interrupt since the last reset). Neither is a calibrated threshold.
* State is derived, never stored: append-only ``BreakerEvent`` rows give "tripped" (the last ``TRIPPED`` is later
  than the last ``RESET``) and the set of quarantined bindings. Only a human resets the breaker or lifts a
  quarantine; nothing resets with time.
* Startup marking selects attempts that were live when the process died and moves them to ``INTERRUPTED``
  exactly once: an interrupted attempt is terminal and is never selected again, so applying the result and
  running the function again returns nothing.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum

from studio.domain.canonical import SAFE_INT_MAX
from studio.domain.errors import fail
from studio.domain.ids import (
    AttemptId,
    BreakerEventId,
    check_id,
    check_registry_id,
    check_text,
    check_unique_ids,
)
from studio.domain.recovery import AttemptEndCause, counts_toward_breaker
from studio.domain.states import JOB_TRANSITIONS, AttemptState, JobState
from studio.domain.workflow import Attempt, Job

# While K is unset the breaker is equivalent to K = 1 (design C.6): a safety rule, not a threshold.
FAIL_CLOSED_K = 1


def _check_aware(value: datetime, path: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        fail("time.naive", path, "timestamps must be timezone-aware")


def _check_limit(value: object, path: str, code: str) -> None:
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= SAFE_INT_MAX
    ):
        fail(code, path, "must be an integer >= 1, or None when not configured")


class BreakerEventKind(StrEnum):
    TRIPPED = "tripped"
    RESET = "reset"
    QUARANTINE_LIFTED = "quarantine_lifted"


@dataclass(frozen=True, kw_only=True)
class BreakerEvent:
    """One append-only breaker event. ``TRIPPED`` is recorded by the scheduler and names the quarantined
    registry binding and the counted interrupts, with the ``k`` and ``window_s`` in force (``None`` = not
    configured, fail-closed). ``RESET`` and ``QUARANTINE_LIFTED`` are human actions and name the person."""

    event_id: BreakerEventId
    kind: BreakerEventKind
    at: datetime
    by_name: str | None = None
    binding_id: str | None = None
    attempt_ids: tuple[AttemptId, ...] = ()
    k: int | None = None
    window_s: int | None = None

    def __post_init__(self) -> None:
        check_id(self.event_id, "breaker.event_id")
        _check_aware(self.at, "breaker.at")
        _check_limit(self.k, "breaker.k", "breaker.k")
        _check_limit(self.window_s, "breaker.window_s", "breaker.window_s")
        object.__setattr__(
            self, "attempt_ids", check_unique_ids(self.attempt_ids, "breaker.attempt_ids")
        )
        if self.by_name is not None:
            check_text(self.by_name, "breaker.by_name")
        if self.binding_id is not None:
            check_registry_id(self.binding_id, "breaker.binding_id")
        if self.kind is BreakerEventKind.TRIPPED:
            if self.by_name is not None:
                fail(
                    "breaker.actor",
                    "breaker.by_name",
                    "the scheduler trips the breaker, not a person",
                )
            if self.binding_id is None or not self.attempt_ids:
                fail(
                    "breaker.trip",
                    "breaker",
                    "a trip names the quarantined binding and the counted interrupts",
                )
            return
        if self.by_name is None:
            fail(
                "breaker.actor",
                "breaker.by_name",
                f"{self.kind} is a human action and names the person",
            )
        if self.attempt_ids or self.k is not None or self.window_s is not None:
            fail(
                "breaker.fields", "breaker", f"{self.kind} carries no counted interrupts or limits"
            )
        if self.kind is BreakerEventKind.RESET and self.binding_id is not None:
            fail("breaker.fields", "breaker.binding_id", "a reset names no binding")
        if self.kind is BreakerEventKind.QUARANTINE_LIFTED and self.binding_id is None:
            fail("breaker.fields", "breaker.binding_id", "lifting a quarantine names the binding")


@dataclass(frozen=True, kw_only=True)
class BreakerVerdict:
    tripped: bool  # the breaker is currently tripped (a human has not reset it)
    count_in_window: int  # counted interrupts after the last reset and inside the window
    should_trip: bool  # not tripped yet and the count reached K (fail-closed when K is unset)
    quarantined: frozenset[str]  # registry bindings under quarantine (TRIPPED without a later lift)
    counted_attempt_ids: tuple[AttemptId, ...] = ()  # the counted interrupts, oldest first


def breaker_verdict(
    interrupts: Iterable[Attempt],
    events: Iterable[BreakerEvent],
    *,
    k: int | None,
    window_s: int | None,
    now: datetime,
) -> BreakerVerdict:
    """The breaker state at ``now``, derived from attempt rows and append-only events.

    A counted interrupt is an attempt for which ``counts_toward_breaker`` holds, interrupted at ``finished_at``
    with ``now - window_s <= finished_at <= now`` and strictly after the last ``RESET``. The window edges are
    inclusive. Duplicate rows of one attempt count once.
    """
    _check_aware(now, "breaker.now")
    _check_limit(k, "breaker.k", "breaker.k")
    _check_limit(window_s, "breaker.window_s", "breaker.window_s")

    tripped = False
    last_reset: datetime | None = None
    quarantined: set[str] = set()
    for event in sorted(events, key=lambda e: e.at):
        if event.kind is BreakerEventKind.TRIPPED:
            tripped = True
            if event.binding_id is not None:
                quarantined.add(event.binding_id)
        elif event.kind is BreakerEventKind.RESET:
            tripped = False
            last_reset = event.at
        elif event.binding_id is not None:
            quarantined.discard(event.binding_id)

    earliest = None if window_s is None else now - timedelta(seconds=window_s)
    counted: dict[AttemptId, datetime] = {}
    for attempt in interrupts:
        if not counts_toward_breaker(attempt) or attempt.finished_at is None:
            continue
        at = attempt.finished_at
        if at > now or (earliest is not None and at < earliest):
            continue
        if last_reset is not None and at <= last_reset:
            continue
        counted[attempt.attempt_id] = at

    threshold = FAIL_CLOSED_K if k is None else k
    return BreakerVerdict(
        tripped=tripped,
        count_in_window=len(counted),
        should_trip=not tripped and len(counted) >= threshold,
        quarantined=frozenset(quarantined),
        counted_attempt_ids=tuple(
            attempt_id for attempt_id, _ in sorted(counted.items(), key=lambda item: item[1])
        ),
    )


# ---------------------------------------------------------------- startup interruption marking

_LIVE_ATTEMPT_STATES = frozenset(
    {AttemptState.PENDING, AttemptState.RUNNING, AttemptState.INGESTING}
)


@dataclass(frozen=True, kw_only=True)
class AttemptInterruption:
    """Mark one live attempt INTERRUPTED. ``finished_at`` is the interruption time (``now`` of the marking)."""

    attempt_id: AttemptId
    source: AttemptState
    cause: AttemptEndCause
    finished_at: datetime
    interrupted_boot_id: str


@dataclass(frozen=True, kw_only=True)
class JobTransition:
    job_id: str
    source: JobState
    target: JobState


type StartupTransition = AttemptInterruption | JobTransition


def startup_interruptions(
    jobs: Iterable[Job],
    attempts: Iterable[Attempt],
    *,
    boot_id: str,
    host_boot_at: datetime,
    now: datetime,
) -> tuple[StartupTransition, ...]:
    """What to change after a (re)start so no row claims work that no process is doing. Idempotent.

    * Every attempt in PENDING, RUNNING or INGESTING becomes INTERRUPTED with cause ``HOST_RESTART`` if it had
      started before the host booted (``started_at < host_boot_at``), otherwise ``STUDIO_PROCESS`` (also for an
      attempt that never started). ``finished_at`` is ``now`` and ``interrupted_boot_id`` is ``boot_id``.
    * A job in RUNNING or INGESTING becomes INTERRUPTED; a job in WAITING_RESOURCES that has a PENDING attempt
      becomes READY (so it is admitted again).
    * INTERRUPTED attempts are terminal and never selected again, so applying the result and calling this
      again returns an empty tuple. The caller applies everything in one transaction keyed by ``boot_id``.

    Raises ``DomainValidationError`` if an attempt claims to have started after ``now`` (clock skew).
    """
    check_text(boot_id, "startup.boot_id")
    _check_aware(host_boot_at, "startup.host_boot_at")
    _check_aware(now, "startup.now")
    rows = sorted(attempts, key=lambda a: (a.job_id, a.number, a.attempt_id))
    result: list[StartupTransition] = []
    pending_jobs: set[str] = set()
    for attempt in rows:
        if attempt.state not in _LIVE_ATTEMPT_STATES:
            continue
        if attempt.started_at is not None and attempt.started_at > now:
            fail(
                "startup.clock",
                f"attempts[{attempt.attempt_id}]",
                "the attempt started after the time of the marking",
            )
        host_restart = attempt.started_at is not None and attempt.started_at < host_boot_at
        result.append(
            AttemptInterruption(
                attempt_id=attempt.attempt_id,
                source=attempt.state,
                cause=(
                    AttemptEndCause.HOST_RESTART if host_restart else AttemptEndCause.STUDIO_PROCESS
                ),
                finished_at=now,
                interrupted_boot_id=boot_id,
            )
        )
        if attempt.state is AttemptState.PENDING:
            pending_jobs.add(attempt.job_id)
    for job in sorted(jobs, key=lambda j: j.job_id):
        if job.state in (JobState.RUNNING, JobState.INGESTING):
            result.append(
                JobTransition(job_id=job.job_id, source=job.state, target=JobState.INTERRUPTED)
            )
        elif job.state is JobState.WAITING_RESOURCES and job.job_id in pending_jobs:
            result.append(JobTransition(job_id=job.job_id, source=job.state, target=JobState.READY))
    return tuple(result)


def apply_attempt_interruption(attempt: Attempt, change: AttemptInterruption) -> Attempt:
    """The attempt after the marking (re-validated by its constructor). Pure; persistence stores the result."""
    if attempt.attempt_id != change.attempt_id or attempt.state is not change.source:
        fail(
            "startup.apply",
            f"attempts[{attempt.attempt_id}]",
            "the change is for another attempt state",
        )
    return replace(
        attempt,
        state=AttemptState.INTERRUPTED,
        cause=change.cause,
        finished_at=change.finished_at,
        interrupted_boot_id=change.interrupted_boot_id,
    )


def apply_job_transition(job: Job, change: JobTransition) -> Job:
    """The job after the transition, checked against the job transition table."""
    if job.job_id != change.job_id or job.state is not change.source:
        fail("startup.apply", f"jobs[{job.job_id}]", "the change is for another job state")
    JOB_TRANSITIONS.require_transition(job.state, change.target)
    return replace(job, state=change.target)
