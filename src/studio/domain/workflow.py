"""Workflow domain: task specification, job, attempt, DAG nodes/dependencies, approvals and gates.

Data contracts only. There is no scheduler, executor or readiness logic here; the graph helpers answer
structural questions (is it acyclic, what is a deterministic order) and nothing else. Human approval gates are
DAG nodes that hold no GPU lease (architecture section 2).
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from studio.domain.errors import ValidationIssue, fail
from studio.domain.fingerprint import (
    EngineComponent,
    Fingerprint,
    FingerprintKind,
    ModelComponent,
    compute_fingerprint,
    compute_task_fingerprint,
)
from studio.domain.graph import topological_order
from studio.domain.ids import (
    ROLE_RE,
    ApprovalId,
    AttemptId,
    GateId,
    JobId,
    ProjectId,
    TakeId,
    VersionId,
    check_id,
    check_optional_id,
    check_sha256,
    check_text,
    check_unique_ids,
)
from studio.domain.production import CreativeSpec, ExecutionSpec
from studio.domain.recovery import (
    AttemptEndCause,
    AttemptInitiator,
    AttemptOrigin,
    ErrorClass,
    JobFailure,
    RecoveryPolicySnapshot,
)
from studio.domain.states import (
    ATTEMPT_TRANSITIONS,
    JOB_TRANSITIONS,
    RESOLVED_REQUIRED_JOB_STATES,
    ApprovalState,
    AttemptState,
    GateState,
    JobState,
)


def _set(obj: object, name: str, value: Any) -> None:
    object.__setattr__(obj, name, value)


def _check_aware(value: datetime | None, path: str) -> None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        fail("time.naive", path, "timestamps must be timezone-aware")


# ---------------------------------------------------------------- task specification


@dataclass(frozen=True, kw_only=True)
class TaskSpec:
    """One unit of work for one capability: a ``CreativeSpec`` plus an ``ExecutionSpec``.

    The creative spec alone makes the identity (``identity_hash``, drives approval staleness); the task
    fingerprint adds the execution parameters, models, template and engine (drives reproduction and caching).
    ``creative.binding_id`` names the registry binding (capability); the domain never looks it up. A task is
    *resolved* (``is_resolved``) when its seed, capability and references are fixed; the architecture requires
    that before the first attempt.

    ``execution_only_reserved`` is the set of reserved creative fields (``width``, ``height``, ``duration_s``)
    that the binding declares execution-only (UD-6); the registry list is passed in by the caller.
    """

    creative: CreativeSpec
    execution: ExecutionSpec = field(default_factory=ExecutionSpec)

    @property
    def is_resolved(self) -> bool:
        return self.creative.is_resolved

    def identity_payload(self, *, execution_only_reserved: Collection[str] = ()) -> dict[str, Any]:
        """Payload of the creative identity hash. Raises ``UnresolvedSpecError`` when not resolved."""
        return self.creative.identity_payload(execution_only_reserved=execution_only_reserved)

    def execution_payload(self, *, execution_only_reserved: Collection[str] = ()) -> dict[str, Any]:
        """The execution component of the task fingerprint: the execution parameters plus the reserved
        creative fields that the binding declared execution-only (moved out of the identity)."""
        return {
            "parameters": self.execution.parameters,
            "reserved": self.creative.execution_reserved_values(execution_only_reserved),
        }

    def identity_fingerprint(self, *, execution_only_reserved: Collection[str] = ()) -> Fingerprint:
        return compute_fingerprint(
            FingerprintKind.CREATIVE_IDENTITY,
            self.identity_payload(execution_only_reserved=execution_only_reserved),
        )

    def task_fingerprint(
        self,
        *,
        models: Iterable[ModelComponent],
        template_sha256: str | None,
        engine: EngineComponent,
        execution_only_reserved: Collection[str] = (),
    ) -> Fingerprint:
        """Identity, execution, models, template and engine as one fingerprint (``complete`` only if every
        component is known)."""
        return compute_task_fingerprint(
            identity=self.identity_fingerprint(execution_only_reserved=execution_only_reserved),
            execution=self.execution_payload(execution_only_reserved=execution_only_reserved),
            models=models,
            template_sha256=template_sha256,
            engine=engine,
        )


# ---------------------------------------------------------------- jobs and attempts


@dataclass(frozen=True, kw_only=True)
class Job:
    """A schedulable unit: execute one task, optionally producing a take. Its attempts are separate records.

    ``recovery_policy`` is the snapshot of the recovery limits taken when the job was created. It is required
    and has no default: with ``RecoveryPolicySnapshot.no_automatic_recovery()`` nothing is retried or resumed
    automatically (UD-7). ``failure`` records why a job failed without a failing attempt (admission refusal);
    see ``studio.domain.recovery.permitted_take_recovery``.
    """

    job_id: JobId
    project_id: ProjectId
    task: TaskSpec
    state: JobState = JobState.QUEUED
    take_id: TakeId | None = None  # at most one job per take
    identity_hash: Fingerprint | None = None  # persisted with the resolved task
    task_fingerprint: Fingerprint | None = None  # persisted with the resolved task
    recovery_policy: RecoveryPolicySnapshot
    failure: JobFailure | None = None

    def __post_init__(self) -> None:
        check_id(self.job_id, "job.job_id")
        check_id(self.project_id, "job.project_id")
        check_optional_id(self.take_id, "job.take_id")
        for name, kind in (
            ("identity_hash", FingerprintKind.CREATIVE_IDENTITY),
            ("task_fingerprint", FingerprintKind.TASK_FINGERPRINT),
        ):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, Fingerprint) or value.kind is not kind):
                fail(f"job.{name}", f"job.{name}", f"expected a {kind} fingerprint")
        if not isinstance(self.recovery_policy, RecoveryPolicySnapshot):
            fail(
                "job.recovery_policy",
                "job.recovery_policy",
                "a recovery policy snapshot is required",
            )
        if self.failure is not None:
            if not isinstance(self.failure, JobFailure):
                fail("job.failure", "job.failure", "expected a JobFailure")
            if self.state is not JobState.FAILED:
                fail("job.failure", "job.failure", "only a failed job carries a failure record")
        if self.state in RESOLVED_REQUIRED_JOB_STATES:
            if not self.task.is_resolved:
                fail(
                    "job.unresolved",
                    "job.task",
                    f"a {self.state} job needs a resolved task (seed persisted)",
                )
            if self.identity_hash is None or self.task_fingerprint is None:
                fail(
                    "job.fingerprints",
                    "job.identity_hash",
                    f"a {self.state} job needs its identity_hash and task_fingerprint",
                )


# Attempt states that imply the engine began work, so ``started_at`` is required (see ``Attempt``).
_ENGINE_RAN_STATES = frozenset(
    {
        AttemptState.RUNNING,
        AttemptState.INGESTING,
        AttemptState.SUCCEEDED,
        AttemptState.FAILED,
    }
)


@dataclass(frozen=True, kw_only=True)
class Attempt:
    """One execution try of a job. Created by infrastructure-level events only (first run, retry, resume,
    or a human manual restart).

    A failed or interrupted attempt says who ended it (``cause``): only an ENGINE-side end consumes the
    recovery budget, and only STUDIO_PROCESS/HOST_RESTART interruptions of a started attempt count toward the
    crash-loop breaker. An INTERRUPTED attempt carries the time it was interrupted in ``finished_at``.
    ``artifact_version_ids`` lists intermediate, resumable stage artifacts (terminal states only);
    a RESUME attempt pins the reused artifact's bytes in ``resume_artifact_sha256``.

    ``started_at`` is the moment the engine began, and it decides what an interruption means (an attempt that
    never started is not a recovery event). The rule per state:

    * PENDING: forbidden (nothing has run yet);
    * RUNNING, INGESTING, SUCCEEDED, FAILED: required (the engine ran). A failure at launch, the table edge
      PENDING -> FAILED, therefore stamps ``started_at`` when it fails;
    * INTERRUPTED: optional, and ``None`` means *never started* (an ENGINE cause still requires it);
    * CANCELED: optional.
    """

    attempt_id: AttemptId
    job_id: JobId
    number: int  # 1-based, contiguous within the job
    initiated_by: AttemptInitiator
    state: AttemptState = AttemptState.PENDING
    origin: AttemptOrigin = AttemptOrigin.INITIAL
    initiated_by_name: str | None = None  # HUMAN only
    previous_attempt_id: AttemptId | None = None
    resume_artifact_version_id: VersionId | None = None  # the reused stage artifact (RESUME only)
    resume_artifact_sha256: str | None = None  # RESUME only: pins the reused artifact's bytes
    error_class: ErrorClass | None = None
    cause: AttemptEndCause | None = None  # required iff FAILED or INTERRUPTED
    output_version_ids: tuple[VersionId, ...] = ()
    artifact_version_ids: tuple[VersionId, ...] = ()  # intermediate resumable artifacts
    interrupted_boot_id: str | None = None  # set by startup marking
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def __post_init__(self) -> None:
        check_id(self.attempt_id, "attempt.attempt_id")
        check_id(self.job_id, "attempt.job_id")
        if isinstance(self.number, bool) or not isinstance(self.number, int) or self.number < 1:
            fail("attempt.number", "attempt.number", "must be an integer >= 1")
        check_optional_id(self.previous_attempt_id, "attempt.previous_attempt_id")
        check_optional_id(self.resume_artifact_version_id, "attempt.resume_artifact_version_id")
        _set(
            self,
            "output_version_ids",
            check_unique_ids(self.output_version_ids, "attempt.output_version_ids"),
        )
        _set(
            self,
            "artifact_version_ids",
            check_unique_ids(self.artifact_version_ids, "attempt.artifact_version_ids"),
        )
        if set(self.output_version_ids) & set(self.artifact_version_ids):
            fail(
                "attempt.artifacts",
                "attempt.artifact_version_ids",
                "an output is not also an intermediate artifact",
            )
        _check_aware(self.started_at, "attempt.started_at")
        _check_aware(self.finished_at, "attempt.finished_at")
        if self.started_at and self.finished_at and self.finished_at < self.started_at:
            fail("attempt.time", "attempt.finished_at", "finished before it started")
        if self.state is AttemptState.PENDING:
            if self.started_at is not None:
                fail(
                    "attempt.started_forbidden",
                    "attempt.started_at",
                    "a pending attempt has not started",
                )
        elif self.state in _ENGINE_RAN_STATES and self.started_at is None:
            fail(
                "attempt.started_required",
                "attempt.started_at",
                f"a {self.state} attempt records when it started",
            )

        if self.origin is AttemptOrigin.INITIAL:
            if self.number != 1 or self.previous_attempt_id is not None:
                fail(
                    "attempt.origin",
                    "attempt.origin",
                    "the initial attempt is number 1 with no predecessor",
                )
        else:
            if self.number <= 1 or self.previous_attempt_id is None:
                fail(
                    "attempt.origin", "attempt.origin", f"{self.origin} follows an earlier attempt"
                )
            if self.previous_attempt_id == self.attempt_id:
                fail(
                    "attempt.origin",
                    "attempt.previous_attempt_id",
                    "an attempt cannot follow itself",
                )

        human = self.origin is AttemptOrigin.MANUAL_RESTART
        if human != (self.initiated_by is AttemptInitiator.HUMAN):
            fail(
                "attempt.initiator",
                "attempt.initiated_by",
                "a manual restart is initiated by a human, every other origin automatically",
            )
        if human:
            if self.initiated_by_name is None:
                fail(
                    "attempt.initiator",
                    "attempt.initiated_by_name",
                    "a human-initiated attempt names the person",
                )
            check_text(self.initiated_by_name, "attempt.initiated_by_name")
        elif self.initiated_by_name is not None:
            fail(
                "attempt.initiator",
                "attempt.initiated_by_name",
                "only a human-initiated attempt names a person",
            )

        if self.origin is AttemptOrigin.RESUME:
            if self.resume_artifact_version_id is None or self.resume_artifact_sha256 is None:
                fail(
                    "attempt.resume",
                    "attempt.resume_artifact_version_id",
                    "resume needs the reused artifact and its pinned hash",
                )
            check_sha256(self.resume_artifact_sha256, "attempt.resume_artifact_sha256")
        elif self.resume_artifact_version_id is not None or self.resume_artifact_sha256 is not None:
            fail(
                "attempt.resume",
                "attempt.resume_artifact_version_id",
                "only a resume attempt reuses an artifact",
            )

        terminal = ATTEMPT_TRANSITIONS.is_terminal(self.state)
        if self.artifact_version_ids and not terminal:
            fail(
                "attempt.artifacts",
                "attempt.artifact_version_ids",
                "intermediate artifacts are recorded when the attempt ends",
            )

        if self.state is AttemptState.FAILED and self.error_class is None:
            fail(
                "attempt.error_class", "attempt.error_class", "a failed attempt must be classified"
            )
        if self.error_class is not None and self.state not in (
            AttemptState.FAILED,
            AttemptState.INTERRUPTED,
        ):
            fail(
                "attempt.error_class",
                "attempt.error_class",
                "only failed/interrupted attempts carry one",
            )

        ended_badly = self.state in (AttemptState.FAILED, AttemptState.INTERRUPTED)
        if ended_badly and self.cause is None:
            fail("attempt.cause", "attempt.cause", f"a {self.state} attempt needs its cause")
        if not ended_badly and self.cause is not None:
            fail("attempt.cause", "attempt.cause", "only failed/interrupted attempts have a cause")
        if self.state is AttemptState.FAILED and self.cause is not AttemptEndCause.ENGINE:
            fail("attempt.cause", "attempt.cause", "a failed attempt failed on the engine side")
        if self.state is AttemptState.INTERRUPTED:
            if self.finished_at is None:
                fail(
                    "attempt.interrupted_time",
                    "attempt.finished_at",
                    "an interrupted attempt records when it was interrupted",
                )
            if self.started_at is None and self.cause is AttemptEndCause.ENGINE:
                fail(
                    "attempt.cause_unstarted",
                    "attempt.cause",
                    "an attempt that never started cannot have been ended by the engine",
                )
        if self.interrupted_boot_id is not None:
            if self.state is not AttemptState.INTERRUPTED:
                fail(
                    "attempt.boot",
                    "attempt.interrupted_boot_id",
                    "only an interrupted attempt has an interrupting boot",
                )
            check_text(self.interrupted_boot_id, "attempt.interrupted_boot_id")

        if self.state is AttemptState.SUCCEEDED and not self.output_version_ids:
            fail(
                "attempt.outputs",
                "attempt.output_version_ids",
                "a succeeded attempt produced outputs",
            )
        if self.state is not AttemptState.SUCCEEDED and self.output_version_ids:
            fail(
                "attempt.outputs",
                "attempt.output_version_ids",
                "only a succeeded attempt has outputs",
            )


def check_job_transition(
    source: JobState, target: JobState, *, failure: JobFailure | None = None
) -> None:
    """Table check plus the failure rule: a job that fails while ``WAITING_RESOURCES`` (permanently
    inadmissible) has no attempt and no error class, so it needs a ``JobFailure`` of class RESOURCE
    that names the admission refusal."""
    JOB_TRANSITIONS.require_transition(source, target)
    if source is JobState.WAITING_RESOURCES and target is JobState.FAILED:
        if (
            failure is None
            or failure.error_class is not ErrorClass.RESOURCE
            or failure.refusal is None
        ):
            fail(
                "job.failure_required",
                "job.failure",
                "failing from waiting_resources needs a RESOURCE failure with an admission refusal",
            )


# ---------------------------------------------------------------- approvals and gates


class ApprovalSubjectKind(StrEnum):
    TAKE = "take"
    ASSET_VERSION = "asset_version"
    GATE = "gate"


class ApprovalPurpose(StrEnum):
    CONTENT_REVIEW = "content_review"  # is this output acceptable?
    RESOURCE_ADAPTATION = "resource_adaptation"  # may the task be run in an adapted form?
    GATE_DECISION = "gate_decision"  # pass an approval gate


class ApprovalAuthority(StrEnum):
    HUMAN = "human"
    PRE_AUTHORIZED_POLICY = (
        "pre_authorized_policy"  # which policies exist is undecided (ADR-0010 U-1)
    )


_PURPOSE_SUBJECTS: dict[ApprovalPurpose, frozenset[ApprovalSubjectKind]] = {
    ApprovalPurpose.CONTENT_REVIEW: frozenset(
        {ApprovalSubjectKind.TAKE, ApprovalSubjectKind.ASSET_VERSION}
    ),
    ApprovalPurpose.RESOURCE_ADAPTATION: frozenset({ApprovalSubjectKind.TAKE}),
    ApprovalPurpose.GATE_DECISION: frozenset({ApprovalSubjectKind.GATE}),
}


@dataclass(frozen=True, kw_only=True)
class ApprovalSubject:
    kind: ApprovalSubjectKind
    subject_id: str

    def __post_init__(self) -> None:
        check_id(self.subject_id, "approval.subject.subject_id")


@dataclass(frozen=True, kw_only=True)
class Approval:
    """A recorded decision request/decision. Decisions are never edited: a different decision is a new
    record, and a superseded one becomes ``STALE`` (append-only history, ADR-0004)."""

    approval_id: ApprovalId
    subject: ApprovalSubject
    purpose: ApprovalPurpose
    state: ApprovalState = ApprovalState.PENDING
    authority: ApprovalAuthority = ApprovalAuthority.HUMAN
    policy_ref: str | None = (
        None  # names the pre-authorizing policy; the domain does not define policies
    )
    decided_by: str | None = None
    decided_at: datetime | None = None
    comment: str = ""
    stale_reason: str | None = None

    def __post_init__(self) -> None:
        check_id(self.approval_id, "approval.approval_id")
        if self.subject.kind not in _PURPOSE_SUBJECTS[self.purpose]:
            fail(
                "approval.subject",
                "approval.subject",
                f"{self.purpose} cannot apply to a {self.subject.kind}",
            )
        _check_aware(self.decided_at, "approval.decided_at")
        check_text(self.comment, "approval.comment", allow_empty=True)
        for name in ("policy_ref", "decided_by", "stale_reason"):
            value = getattr(self, name)
            if value is not None:
                check_text(value, f"approval.{name}")
        if self.authority is ApprovalAuthority.PRE_AUTHORIZED_POLICY and self.policy_ref is None:
            fail(
                "approval.policy",
                "approval.policy_ref",
                "a pre-authorized decision must cite its policy",
            )
        if self.authority is ApprovalAuthority.HUMAN and self.policy_ref is not None:
            fail("approval.policy", "approval.policy_ref", "a human decision cites no policy")
        decided = self.state in (ApprovalState.APPROVED, ApprovalState.REJECTED)
        if self.state is ApprovalState.PENDING and (
            self.decided_at is not None or self.decided_by is not None
        ):
            fail("approval.pending", "approval", "a pending approval has no decision data")
        if decided:
            if self.decided_at is None:
                fail("approval.decided_at", "approval.decided_at", "a decision needs a timestamp")
            if self.authority is ApprovalAuthority.HUMAN and self.decided_by is None:
                fail(
                    "approval.decided_by", "approval.decided_by", "a human decision needs a decider"
                )
        if self.state is ApprovalState.STALE and self.stale_reason is None:
            fail("approval.stale", "approval.stale_reason", "a stale approval must say why")
        if self.state is not ApprovalState.STALE and self.stale_reason is not None:
            fail(
                "approval.stale",
                "approval.stale_reason",
                "only a stale approval has a stale_reason",
            )


@dataclass(frozen=True, kw_only=True)
class Gate:
    """A human approval gate: a DAG node passed by a human or a pre-authorized policy decision."""

    gate_id: GateId
    project_id: ProjectId
    title: str
    state: GateState = GateState.BLOCKED
    approval_id: ApprovalId | None = None  # the decision record, once one exists

    def __post_init__(self) -> None:
        check_id(self.gate_id, "gate.gate_id")
        check_id(self.project_id, "gate.project_id")
        check_text(self.title, "gate.title")
        check_optional_id(self.approval_id, "gate.approval_id")
        if self.state in (GateState.PASSED, GateState.REJECTED) and self.approval_id is None:
            fail(
                "gate.approval",
                "gate.approval_id",
                f"a {self.state} gate needs its decision record",
            )


# ---------------------------------------------------------------- DAG


class NodeKind(StrEnum):
    JOB = "job"
    GATE = "gate"


class DependencyKind(StrEnum):
    ORDER = "order"  # downstream waits for upstream (job success / gate pass)
    DATA = "data"  # additionally, an output role of upstream feeds an input role of downstream (job -> job)


@dataclass(frozen=True, kw_only=True)
class NodeRef:
    kind: NodeKind
    node_id: str

    def __post_init__(self) -> None:
        check_id(self.node_id, "node.node_id")

    @property
    def key(self) -> str:
        return f"{self.kind.value}:{self.node_id}"


@dataclass(frozen=True, kw_only=True)
class Dependency:
    upstream: NodeRef
    downstream: NodeRef
    kind: DependencyKind = DependencyKind.ORDER
    output_role: str | None = None  # DATA only
    input_role: str | None = None  # DATA only

    def __post_init__(self) -> None:
        if self.upstream == self.downstream:
            fail("dependency.self", "dependency", "a node cannot depend on itself")
        if self.kind is DependencyKind.DATA:
            if self.upstream.kind is not NodeKind.JOB or self.downstream.kind is not NodeKind.JOB:
                fail("dependency.data_kind", "dependency", "data dependencies connect jobs to jobs")
            if self.output_role is None or self.input_role is None:
                fail(
                    "dependency.roles",
                    "dependency",
                    "a data dependency names an output and an input role",
                )
            check_id(self.output_role, "dependency.output_role", ROLE_RE)
            check_id(self.input_role, "dependency.input_role", ROLE_RE)
        elif self.output_role is not None or self.input_role is not None:
            fail("dependency.roles", "dependency", "an order dependency carries no roles")


@dataclass(frozen=True, kw_only=True)
class WorkflowGraph:
    """Nodes and dependencies of one workflow. Structural queries only."""

    nodes: tuple[NodeRef, ...]
    dependencies: tuple[Dependency, ...] = ()

    def __post_init__(self) -> None:
        _set(self, "nodes", tuple(self.nodes))
        _set(self, "dependencies", tuple(self.dependencies))

    def validate(self) -> tuple[ValidationIssue, ...]:
        issues: list[ValidationIssue] = []
        keys = [n.key for n in self.nodes]
        known = set(keys)
        for key in sorted({k for k in keys if keys.count(k) > 1}):
            issues.append(ValidationIssue("dag.duplicate_node", key, "node listed more than once"))
        seen: set[tuple[str, str, str, str | None, str | None]] = set()
        edges: list[tuple[str, str]] = []
        for dep in self.dependencies:
            label = f"{dep.upstream.key}->{dep.downstream.key}"
            for end in (dep.upstream, dep.downstream):
                if end.key not in known:
                    issues.append(
                        ValidationIssue("dag.unknown_node", label, f"{end.key} is not a node")
                    )
            signature = (
                dep.upstream.key,
                dep.downstream.key,
                dep.kind.value,
                dep.output_role,
                dep.input_role,
            )
            if signature in seen:
                issues.append(
                    ValidationIssue("dag.duplicate_dependency", label, "duplicate dependency")
                )
            seen.add(signature)
            edges.append((dep.upstream.key, dep.downstream.key))
        _, remaining = topological_order(known, edges)
        if remaining:
            issues.append(
                ValidationIssue("dag.cycle", "graph", f"cycle among nodes: {', '.join(remaining)}")
            )
        return tuple(issues)

    def topological_order(self) -> tuple[NodeRef, ...]:
        """Deterministic order (ties by node key). Raises ``DomainValidationError`` on a cycle."""
        by_key = {n.key: n for n in self.nodes}
        order, remaining = topological_order(
            by_key, ((d.upstream.key, d.downstream.key) for d in self.dependencies)
        )
        if remaining:
            fail("dag.cycle", "graph", f"cycle among nodes: {', '.join(remaining)}")
        return tuple(by_key[k] for k in order)

    def upstream_of(self, node: NodeRef) -> tuple[NodeRef, ...]:
        return tuple(
            sorted(
                {d.upstream for d in self.dependencies if d.downstream == node}, key=lambda n: n.key
            )
        )

    def downstream_of(self, node: NodeRef) -> tuple[NodeRef, ...]:
        return tuple(
            sorted(
                {d.downstream for d in self.dependencies if d.upstream == node}, key=lambda n: n.key
            )
        )
