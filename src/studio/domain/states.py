"""State vocabularies and transition tables. Pure data and pure checks; nothing here changes any state.

A ``TransitionTable`` answers "is A -> B allowed?" and "is this state terminal?". Who performs a transition
(scheduler, executor, reviewer) and when is NOT encoded here; that is the job of later milestones.

Provenance of the vocabularies:
* ``JobState`` is exactly the set in docs/architecture.md section 5.
* The other state sets are the implementer's proposals (ADR-0010, P-1 and following) built from the
  architecture text ("approvals may become stale", "plan apply never deletes ... history", "integrity
  errors block export"). They are NOT approved and may change.

``check_table_integrity`` verifies a table against its enum (every state has a row, every target is a state,
all states reachable from the initial one). It is a pure function meant for the first authorized test run; it is
deliberately not executed at import time.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from studio.domain.errors import InvalidTransition, ValidationIssue


class ProjectState(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    ON_HOLD = "on_hold"
    COMPLETED = "completed"
    ARCHIVED = "archived"  # not the same as "plan apply archives"; this is the project as a whole


class ShotState(StrEnum):
    DRAFT = "draft"  # creative spec still being edited / not resolvable yet
    READY = "ready"  # spec complete, no take in flight
    GENERATING = "generating"  # at least one take planned or generating
    IN_REVIEW = "in_review"  # at least one take produced, awaiting a decision
    APPROVED = "approved"  # the selected take is approved
    CANCELED = "canceled"


class TakeState(StrEnum):
    PLANNED = "planned"  # identity, resolved spec hash and seed exist; no job yet
    GENERATING = "generating"  # its job is not terminal (includes retries and waiting)
    PRODUCED = "produced"  # outputs ingested, awaiting review
    APPROVED = "approved"
    REJECTED = "rejected"
    FAILED = "failed"  # its job ended FAILED (infrastructure retries exhausted or not permitted)
    CANCELED = "canceled"


class JobState(StrEnum):
    QUEUED = "queued"
    BLOCKED = "blocked"
    READY = "ready"
    WAITING_RESOURCES = "waiting_resources"
    RUNNING = "running"
    INGESTING = "ingesting"
    SUCCEEDED = "succeeded"
    RETRY_PENDING = "retry_pending"
    FAILED = "failed"
    CANCELED = "canceled"
    INTERRUPTED = "interrupted"


class AttemptState(StrEnum):
    PENDING = "pending"  # attempt row exists, executor not started
    RUNNING = "running"
    INGESTING = "ingesting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"
    INTERRUPTED = "interrupted"  # studio or engine process vanished; outcome unknown


class ApprovalState(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    STALE = "stale"  # the approved/rejected subject was superseded (e.g. by plan apply)
    CANCELED = "canceled"


class GateState(StrEnum):
    BLOCKED = "blocked"  # upstream not complete
    AWAITING_DECISION = "awaiting_decision"  # open; holds no GPU lease (architecture section 2)
    PASSED = "passed"
    REJECTED = "rejected"
    STALE = "stale"
    CANCELED = "canceled"


class AssetState(StrEnum):
    """Lifecycle of one AssetVersion. The bytes of its object never change in any state.

    ``STAGED`` and ``QUARANTINED`` describe the ingestion protocol of ADR-0006 and may exist only on disk,
    never as database rows; they are modelled so the vocabulary is complete.
    """

    STAGED = "staged"
    QUARANTINED = "quarantined"  # failed probe/validation
    AVAILABLE = "available"
    CORRUPT = "corrupt"  # integrity check (hash/size) failed after commit; blocks export
    RETIRED = "retired"  # excluded from further use; the object is NOT deleted (GC is postponed)


@dataclass(frozen=True, eq=False)
class TransitionTable[S: StrEnum]:
    name: str
    initial: S
    edges: Mapping[S, frozenset[S]]

    def successors(self, state: S) -> frozenset[S]:
        return self.edges.get(state, frozenset())

    def can_transition(self, source: S, target: S) -> bool:
        return target in self.successors(source)

    def require_transition(self, source: S, target: S) -> None:
        if not self.can_transition(source, target):
            raise InvalidTransition(self.name, str(source), str(target))

    def is_terminal(self, state: S) -> bool:
        return state in self.edges and not self.edges[state]

    @property
    def terminal_states(self) -> frozenset[S]:
        return frozenset(s for s, targets in self.edges.items() if not targets)


def _table[S: StrEnum](name: str, initial: S, edges: dict[S, tuple[S, ...]]) -> TransitionTable[S]:
    return TransitionTable(
        name=name,
        initial=initial,
        edges=MappingProxyType({s: frozenset(t) for s, t in edges.items()}),
    )


def check_table_integrity[S: StrEnum](
    table: TransitionTable[S], enum_cls: type[S]
) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []
    states = set(enum_cls)
    for state in states - set(table.edges):
        issues.append(ValidationIssue("table.missing_row", table.name, f"no row for state {state}"))
    for state, targets in table.edges.items():
        if state in targets:
            issues.append(ValidationIssue("table.self_loop", table.name, f"{state} -> {state}"))
        for target in targets - states:
            issues.append(
                ValidationIssue("table.unknown_target", table.name, f"{state} -> {target}")
            )
    reachable = {table.initial}
    frontier = [table.initial]
    while frontier:
        for nxt in table.successors(frontier.pop()):
            if nxt not in reachable:
                reachable.add(nxt)
                frontier.append(nxt)
    for state in states - reachable:
        issues.append(
            ValidationIssue("table.unreachable", table.name, f"{state} unreachable from initial")
        )
    if not table.terminal_states:
        issues.append(ValidationIssue("table.no_terminal", table.name, "no terminal state"))
    return tuple(issues)


PROJECT_TRANSITIONS = _table(
    "project",
    ProjectState.DRAFT,
    {
        ProjectState.DRAFT: (ProjectState.ACTIVE, ProjectState.ARCHIVED),
        ProjectState.ACTIVE: (ProjectState.ON_HOLD, ProjectState.COMPLETED, ProjectState.ARCHIVED),
        ProjectState.ON_HOLD: (ProjectState.ACTIVE, ProjectState.ARCHIVED),
        ProjectState.COMPLETED: (ProjectState.ACTIVE, ProjectState.ARCHIVED),
        ProjectState.ARCHIVED: (),
    },
)

SHOT_TRANSITIONS = _table(
    "shot",
    ShotState.DRAFT,
    {
        ShotState.DRAFT: (ShotState.READY, ShotState.CANCELED),
        ShotState.READY: (ShotState.GENERATING, ShotState.DRAFT, ShotState.CANCELED),
        ShotState.GENERATING: (ShotState.IN_REVIEW, ShotState.READY, ShotState.CANCELED),
        # DRAFT from review/approved = the spec was edited by plan apply (approvals become stale).
        ShotState.IN_REVIEW: (
            ShotState.APPROVED,
            ShotState.GENERATING,
            ShotState.READY,
            ShotState.DRAFT,
            ShotState.CANCELED,
        ),
        ShotState.APPROVED: (
            ShotState.IN_REVIEW,  # approval went stale
            ShotState.GENERATING,  # creative regeneration of an approved shot
            ShotState.DRAFT,
            ShotState.CANCELED,
        ),
        ShotState.CANCELED: (),
    },
)

TAKE_TRANSITIONS = _table(
    "take",
    TakeState.PLANNED,
    {
        TakeState.PLANNED: (TakeState.GENERATING, TakeState.CANCELED),
        TakeState.GENERATING: (TakeState.PRODUCED, TakeState.FAILED, TakeState.CANCELED),
        TakeState.PRODUCED: (TakeState.APPROVED, TakeState.REJECTED),
        TakeState.APPROVED: (
            TakeState.PRODUCED,
            TakeState.REJECTED,
        ),  # PRODUCED = approval went stale
        TakeState.REJECTED: (),
        TakeState.FAILED: (),
        TakeState.CANCELED: (),
    },
)

JOB_TRANSITIONS = _table(
    "job",
    JobState.QUEUED,
    {
        JobState.QUEUED: (JobState.BLOCKED, JobState.READY, JobState.CANCELED),
        JobState.BLOCKED: (JobState.READY, JobState.CANCELED),
        # Every job, CPU or GPU, passes admission (ADR-0008 D-1): READY -> WAITING_RESOURCES -> RUNNING.
        JobState.READY: (JobState.WAITING_RESOURCES, JobState.BLOCKED, JobState.CANCELED),
        JobState.WAITING_RESOURCES: (
            JobState.RUNNING,
            JobState.READY,
            JobState.FAILED,  # permanently inadmissible
            JobState.CANCELED,
        ),
        JobState.RUNNING: (
            JobState.INGESTING,
            JobState.RETRY_PENDING,
            JobState.FAILED,
            JobState.INTERRUPTED,
            JobState.CANCELED,
        ),
        JobState.INGESTING: (
            JobState.SUCCEEDED,
            JobState.RETRY_PENDING,
            JobState.FAILED,
            JobState.INTERRUPTED,
            JobState.CANCELED,
        ),
        JobState.RETRY_PENDING: (JobState.READY, JobState.FAILED, JobState.CANCELED),
        JobState.INTERRUPTED: (JobState.RETRY_PENDING, JobState.FAILED, JobState.CANCELED),
        JobState.SUCCEEDED: (),
        JobState.FAILED: (),
        JobState.CANCELED: (),
    },
)

ATTEMPT_TRANSITIONS = _table(
    "attempt",
    AttemptState.PENDING,
    {
        AttemptState.PENDING: (
            AttemptState.RUNNING,
            AttemptState.FAILED,
            AttemptState.CANCELED,
            AttemptState.INTERRUPTED,
        ),
        AttemptState.RUNNING: (
            AttemptState.INGESTING,
            AttemptState.FAILED,
            AttemptState.CANCELED,
            AttemptState.INTERRUPTED,
        ),
        AttemptState.INGESTING: (
            AttemptState.SUCCEEDED,
            AttemptState.FAILED,
            AttemptState.CANCELED,
            AttemptState.INTERRUPTED,
        ),
        AttemptState.SUCCEEDED: (),
        AttemptState.FAILED: (),
        AttemptState.CANCELED: (),
        AttemptState.INTERRUPTED: (),
    },
)

APPROVAL_TRANSITIONS = _table(
    "approval",
    ApprovalState.PENDING,
    {
        ApprovalState.PENDING: (
            ApprovalState.APPROVED,
            ApprovalState.REJECTED,
            ApprovalState.STALE,
            ApprovalState.CANCELED,
        ),
        # A decision is never edited; a different decision is a new approval record (append-only history).
        ApprovalState.APPROVED: (ApprovalState.STALE,),
        ApprovalState.REJECTED: (ApprovalState.STALE,),
        ApprovalState.STALE: (),
        ApprovalState.CANCELED: (),
    },
)

GATE_TRANSITIONS = _table(
    "gate",
    GateState.BLOCKED,
    {
        GateState.BLOCKED: (GateState.AWAITING_DECISION, GateState.STALE, GateState.CANCELED),
        GateState.AWAITING_DECISION: (
            GateState.PASSED,
            GateState.REJECTED,
            GateState.BLOCKED,  # an upstream node was invalidated
            GateState.STALE,
            GateState.CANCELED,
        ),
        GateState.PASSED: (GateState.STALE,),
        GateState.REJECTED: (GateState.STALE,),
        GateState.STALE: (),
        GateState.CANCELED: (),
    },
)

ASSET_TRANSITIONS = _table(
    "asset_version",
    AssetState.STAGED,
    {
        AssetState.STAGED: (AssetState.AVAILABLE, AssetState.QUARANTINED),
        AssetState.QUARANTINED: (AssetState.RETIRED,),
        AssetState.AVAILABLE: (AssetState.CORRUPT, AssetState.RETIRED),
        AssetState.CORRUPT: (AssetState.RETIRED,),
        AssetState.RETIRED: (),
    },
)

ALL_TABLES: Mapping[str, tuple[TransitionTable, type[StrEnum]]] = MappingProxyType(
    {
        "project": (PROJECT_TRANSITIONS, ProjectState),
        "shot": (SHOT_TRANSITIONS, ShotState),
        "take": (TAKE_TRANSITIONS, TakeState),
        "job": (JOB_TRANSITIONS, JobState),
        "attempt": (ATTEMPT_TRANSITIONS, AttemptState),
        "approval": (APPROVAL_TRANSITIONS, ApprovalState),
        "gate": (GATE_TRANSITIONS, GateState),
        "asset_version": (ASSET_TRANSITIONS, AssetState),
    }
)

# Job states in which the job has (or had) a live attempt in the same phase.
ACTIVE_JOB_STATE_FOR_ATTEMPT: Mapping[AttemptState, JobState] = MappingProxyType(
    {AttemptState.RUNNING: JobState.RUNNING, AttemptState.INGESTING: JobState.INGESTING}
)

# Job states from which the resolved task (and its seed) must already be persisted (architecture section 5).
# FAILED is excluded: a job may fail before resolution (e.g. an invalid spec).
RESOLVED_REQUIRED_JOB_STATES: frozenset[JobState] = frozenset(
    {
        JobState.WAITING_RESOURCES,
        JobState.RUNNING,
        JobState.INGESTING,
        JobState.SUCCEEDED,
        JobState.RETRY_PENDING,
        JobState.INTERRUPTED,
    }
)


def check_all_tables() -> tuple[ValidationIssue, ...]:
    """Integrity check of every table (pure; intended for the first authorized test run)."""
    issues: list[ValidationIssue] = []
    for table, enum_cls in ALL_TABLES.values():
        issues.extend(check_table_integrity(table, enum_cls))
    return tuple(issues)
