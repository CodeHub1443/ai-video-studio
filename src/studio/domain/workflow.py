"""Workflow domain: task specification, job, attempt, DAG nodes/dependencies, approvals and gates.

Data contracts only. There is no scheduler, executor or readiness logic here; the graph helpers answer
structural questions (is it acyclic, what is a deterministic order) and nothing else. Human approval gates are
DAG nodes that hold no GPU lease (architecture section 2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from studio.domain.errors import ValidationIssue, fail
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
from studio.domain.recovery import DEFAULT_MAX_INFRASTRUCTURE_RETRIES, AttemptOrigin, ErrorClass
from studio.domain.states import (
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

    Only ``creative`` is fingerprinted. ``creative.binding_id`` names the registry binding (capability);
    the domain never looks it up. A task is *resolved* (``is_resolved``) when its seed, capability and
    references are fixed; the architecture requires that before the first attempt.
    """

    creative: CreativeSpec
    execution: ExecutionSpec = field(default_factory=ExecutionSpec)

    @property
    def is_resolved(self) -> bool:
        return self.creative.is_resolved

    def fingerprint_input(self) -> dict[str, Any]:
        """The structure a later milestone hashes into ``spec_hash``: resolved creative spec only.
        Raises ``UnresolvedSpecError`` when not resolved. Execution parameters are excluded by design."""
        return self.creative.canonical_form(resolved=True)


# ---------------------------------------------------------------- jobs and attempts


@dataclass(frozen=True, kw_only=True)
class Job:
    """A schedulable unit: execute one task, optionally producing a take. Its attempts are separate records.

    ``max_infrastructure_retries`` bounds attempt-level recovery (infrastructure retry and resume) and is
    provisional (docs/architecture.md section 5).
    """

    job_id: JobId
    project_id: ProjectId
    task: TaskSpec
    state: JobState = JobState.QUEUED
    take_id: TakeId | None = None  # at most one job per take
    spec_hash: str | None = None  # persisted with the resolved task
    max_infrastructure_retries: int = DEFAULT_MAX_INFRASTRUCTURE_RETRIES

    def __post_init__(self) -> None:
        check_id(self.job_id, "job.job_id")
        check_id(self.project_id, "job.project_id")
        check_optional_id(self.take_id, "job.take_id")
        if self.spec_hash is not None:
            check_sha256(self.spec_hash, "job.spec_hash")
        if (
            isinstance(self.max_infrastructure_retries, bool)
            or not isinstance(self.max_infrastructure_retries, int)
            or self.max_infrastructure_retries < 0
        ):
            fail("job.retries", "job.max_infrastructure_retries", "must be an integer >= 0")
        if self.state in RESOLVED_REQUIRED_JOB_STATES:
            if not self.task.is_resolved:
                fail(
                    "job.unresolved",
                    "job.task",
                    f"a {self.state} job needs a resolved task (seed persisted)",
                )
            if self.spec_hash is None:
                fail("job.spec_hash", "job.spec_hash", f"a {self.state} job needs its spec_hash")


@dataclass(frozen=True, kw_only=True)
class Attempt:
    """One execution try of a job. Created by infrastructure-level events only (first run, retry, resume)."""

    attempt_id: AttemptId
    job_id: JobId
    number: int  # 1-based, contiguous within the job
    state: AttemptState = AttemptState.PENDING
    origin: AttemptOrigin = AttemptOrigin.INITIAL
    previous_attempt_id: AttemptId | None = None
    resume_artifact_version_id: VersionId | None = None  # the reused stage-1 artifact (RESUME only)
    error_class: ErrorClass | None = None
    output_version_ids: tuple[VersionId, ...] = ()
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
        _check_aware(self.started_at, "attempt.started_at")
        _check_aware(self.finished_at, "attempt.finished_at")
        if self.started_at and self.finished_at and self.finished_at < self.started_at:
            fail("attempt.time", "attempt.finished_at", "finished before it started")

        if self.origin is AttemptOrigin.INITIAL:
            if self.number != 1 or self.previous_attempt_id is not None:
                fail(
                    "attempt.origin",
                    "attempt.origin",
                    "the initial attempt is number 1 with no predecessor",
                )
        else:
            if self.number < 2 or self.previous_attempt_id is None:
                fail(
                    "attempt.origin", "attempt.origin", f"{self.origin} follows an earlier attempt"
                )
            if self.previous_attempt_id == self.attempt_id:
                fail(
                    "attempt.origin",
                    "attempt.previous_attempt_id",
                    "an attempt cannot follow itself",
                )
        if self.origin is AttemptOrigin.RESUME:
            if self.resume_artifact_version_id is None:
                fail(
                    "attempt.resume",
                    "attempt.resume_artifact_version_id",
                    "resume needs the reused artifact",
                )
        elif self.resume_artifact_version_id is not None:
            fail(
                "attempt.resume",
                "attempt.resume_artifact_version_id",
                "only a resume attempt reuses an artifact",
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
