"""Referential and structural validation of one project's domain objects.

``DomainSnapshot`` is an in-memory bundle of entities; ``validate_snapshot`` returns *all* findings as
``ValidationIssue`` values (it does not stop at the first and never raises for content problems). Field-level
rules live in each entity's constructor; this module checks what needs several entities at once.

Pure: no persistence, no execution, no clock, no I/O. Rules about *policy* (what an unknown license blocks,
who may approve what) are intentionally absent; see ADR-0010 for the open items.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from studio.domain.assets import (
    Asset,
    AssetVersion,
    LineageEdge,
    ProvenanceOrigin,
    check_metadata_for,
    validate_lineage,
)
from studio.domain.errors import ValidationIssue, raise_if_issues
from studio.domain.fingerprint import stage_key
from studio.domain.production import Project, Scene, Shot, Take
from studio.domain.recovery import (
    ATTEMPT_ORIGIN_KIND,
    AttemptEndCause,
    AttemptOrigin,
    TakeOrigin,
    is_recovery_permitted,
    never_started,
    resume_chain_depth,
    resumes_used,
    retries_used,
)
from studio.domain.states import (
    ACTIVE_JOB_STATE_FOR_ATTEMPT,
    ATTEMPT_TRANSITIONS,
    JOB_TRANSITIONS,
    ApprovalState,
    AssetState,
    AttemptState,
    GateState,
    JobState,
    ShotState,
    TakeState,
)
from studio.domain.workflow import (
    Approval,
    ApprovalPurpose,
    ApprovalSubjectKind,
    Attempt,
    Dependency,
    Gate,
    Job,
    NodeKind,
    NodeRef,
    WorkflowGraph,
)


@dataclass(frozen=True, kw_only=True)
class DomainSnapshot:
    """Everything known about ONE project at one moment. Build it from any source; it holds no behaviour."""

    project: Project
    scenes: tuple[Scene, ...] = ()
    shots: tuple[Shot, ...] = ()
    takes: tuple[Take, ...] = ()
    assets: tuple[Asset, ...] = ()
    asset_versions: tuple[AssetVersion, ...] = ()
    lineage: tuple[LineageEdge, ...] = ()
    jobs: tuple[Job, ...] = ()
    attempts: tuple[Attempt, ...] = ()
    gates: tuple[Gate, ...] = ()
    dependencies: tuple[Dependency, ...] = ()
    approvals: tuple[Approval, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "scenes", "shots", "takes", "assets", "asset_versions", "lineage",
            "jobs", "attempts", "gates", "dependencies", "approvals",
        ):  # fmt: skip
            object.__setattr__(self, name, tuple(getattr(self, name)))


def _issue(issues: list[ValidationIssue], code: str, path: str, message: str) -> None:
    issues.append(ValidationIssue(code, path, message))


def _unique(issues: list[ValidationIssue], label: str, ids: Iterable[str]) -> None:
    for value, count in sorted(Counter(ids).items()):
        if count > 1:
            _issue(
                issues, "ref.duplicate_id", f"{label}[{value}]", f"{count} {label} share this id"
            )


def _contiguous(
    issues: list[ValidationIssue], label: str, groups: dict[str, list[int]], what: str
) -> None:
    """Each group's numbers must be exactly 1..n (no duplicates, no gaps)."""
    for owner, numbers in sorted(groups.items()):
        if sorted(numbers) != list(range(1, len(numbers) + 1)):
            _issue(
                issues,
                "ref.numbering",
                f"{label}[{owner}]",
                f"{what} must be unique and contiguous from 1",
            )


def validate_snapshot(snapshot: DomainSnapshot) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []
    for check in (
        _check_ids,
        _check_production,
        _check_assets,
        _check_workflow,
        _check_recovery,
        _check_cross,
    ):
        check(snapshot, issues)
    return tuple(issues)


def ensure_valid_snapshot(snapshot: DomainSnapshot) -> None:
    """Raise ``DomainValidationError`` carrying every issue, or return None."""
    raise_if_issues(validate_snapshot(snapshot))


# ---------------------------------------------------------------- identity and numbering


def _check_ids(s: DomainSnapshot, issues: list[ValidationIssue]) -> None:
    _unique(issues, "scenes", (x.scene_id for x in s.scenes))
    _unique(issues, "shots", (x.shot_id for x in s.shots))
    _unique(issues, "takes", (x.take_id for x in s.takes))
    _unique(issues, "assets", (x.asset_id for x in s.assets))
    _unique(issues, "asset_versions", (x.version_id for x in s.asset_versions))
    _unique(issues, "jobs", (x.job_id for x in s.jobs))
    _unique(issues, "attempts", (x.attempt_id for x in s.attempts))
    _unique(issues, "gates", (x.gate_id for x in s.gates))
    _unique(issues, "approvals", (x.approval_id for x in s.approvals))

    scene_numbers: dict[str, list[int]] = defaultdict(list)
    for scene in s.scenes:
        scene_numbers[scene.project_id].append(scene.ordinal)
    _contiguous(issues, "projects", scene_numbers, "scene ordinals")
    shot_numbers: dict[str, list[int]] = defaultdict(list)
    for shot in s.shots:
        shot_numbers[shot.scene_id].append(shot.ordinal)
    _contiguous(issues, "scenes", shot_numbers, "shot ordinals")
    take_numbers: dict[str, list[int]] = defaultdict(list)
    for take in s.takes:
        take_numbers[take.shot_id].append(take.number)
    _contiguous(issues, "shots", take_numbers, "take numbers")
    version_numbers: dict[str, list[int]] = defaultdict(list)
    for version in s.asset_versions:
        version_numbers[version.asset_id].append(version.version_number)
    _contiguous(issues, "assets", version_numbers, "version numbers")
    attempt_numbers: dict[str, list[int]] = defaultdict(list)
    for attempt in s.attempts:
        attempt_numbers[attempt.job_id].append(attempt.number)
    _contiguous(issues, "jobs", attempt_numbers, "attempt numbers")


# ---------------------------------------------------------------- production


def _check_production(s: DomainSnapshot, issues: list[ValidationIssue]) -> None:
    project_id = s.project.project_id
    scenes = {x.scene_id: x for x in s.scenes}
    shots = {x.shot_id: x for x in s.shots}
    takes = {x.take_id: x for x in s.takes}
    versions = {x.version_id: x for x in s.asset_versions}
    assets = {x.asset_id: x for x in s.assets}
    approvals = {x.approval_id: x for x in s.approvals}

    for scene in s.scenes:
        if scene.project_id != project_id:
            _issue(issues, "ref.project", f"scenes[{scene.scene_id}]", "belongs to another project")
    for shot in s.shots:
        where = f"shots[{shot.shot_id}]"
        if shot.scene_id not in scenes:
            _issue(issues, "ref.missing", f"{where}.scene_id", f"unknown scene {shot.scene_id}")
        _check_asset_refs(issues, where, shot.creative.references, assets, versions)
        shot_takes = [t for t in s.takes if t.shot_id == shot.shot_id]
        if shot.selected_take_id is not None:
            selected = takes.get(shot.selected_take_id)
            if selected is None:
                _issue(
                    issues,
                    "ref.missing",
                    f"{where}.selected_take_id",
                    f"unknown take {shot.selected_take_id}",
                )
            elif selected.shot_id != shot.shot_id:
                _issue(
                    issues,
                    "ref.mismatch",
                    f"{where}.selected_take_id",
                    "take belongs to another shot",
                )
            elif selected.state not in (TakeState.PRODUCED, TakeState.APPROVED):
                _issue(
                    issues,
                    "state.selected_take",
                    f"{where}.selected_take_id",
                    f"selected take is {selected.state}",
                )
        if shot.state is ShotState.APPROVED:
            selected = takes.get(shot.selected_take_id) if shot.selected_take_id else None
            if selected is None or selected.state is not TakeState.APPROVED:
                _issue(
                    issues,
                    "state.shot_approved",
                    where,
                    "an approved shot needs an approved selected take",
                )
        if shot.state is ShotState.IN_REVIEW and not any(
            t.state in (TakeState.PRODUCED, TakeState.APPROVED) for t in shot_takes
        ):
            _issue(issues, "state.shot_review", where, "in review without a produced take")
        if shot.state is ShotState.GENERATING and not any(
            t.state in (TakeState.PLANNED, TakeState.GENERATING) for t in shot_takes
        ):
            _issue(
                issues,
                "state.shot_generating",
                where,
                "generating without a planned or generating take",
            )

    for take in s.takes:
        where = f"takes[{take.take_id}]"
        if take.shot_id not in shots:
            _issue(issues, "ref.missing", f"{where}.shot_id", f"unknown shot {take.shot_id}")
        for version_id in take.output_version_ids:
            if version_id not in versions:
                _issue(
                    issues,
                    "ref.missing",
                    f"{where}.output_version_ids",
                    f"unknown version {version_id}",
                )
        if take.source_take_id is not None:
            _check_take_source(issues, where, take, takes)
        if (
            take.origin is TakeOrigin.RESOURCE_ADAPTATION
            and take.adaptation_approval_id is not None
        ):
            approval = approvals.get(take.adaptation_approval_id)
            if approval is None:
                _issue(issues, "ref.missing", f"{where}.adaptation_approval_id", "unknown approval")
            else:
                if approval.purpose is not ApprovalPurpose.RESOURCE_ADAPTATION or (
                    approval.subject.kind is not ApprovalSubjectKind.TAKE
                    or approval.subject.subject_id != take.take_id
                ):
                    _issue(
                        issues,
                        "ref.mismatch",
                        f"{where}.adaptation_approval_id",
                        "approval is not for this adaptation",
                    )
                if (
                    take.state not in (TakeState.PLANNED, TakeState.CANCELED)
                    and approval.state is not ApprovalState.APPROVED
                ):
                    _issue(
                        issues,
                        "state.adaptation",
                        where,
                        "adapted take progressed without an approved adaptation",
                    )


def _check_take_source(
    issues: list[ValidationIssue], where: str, take: Take, takes: Mapping[Any, Take]
) -> None:
    source = takes.get(take.source_take_id or "")
    if source is None:
        _issue(
            issues, "ref.missing", f"{where}.source_take_id", f"unknown take {take.source_take_id}"
        )
        return
    if source.shot_id != take.shot_id:
        _issue(
            issues, "ref.mismatch", f"{where}.source_take_id", "source take belongs to another shot"
        )
    if take.origin is TakeOrigin.REPRODUCTION:
        if take.identity_hash != source.identity_hash:
            _issue(
                issues,
                "recovery.reproduction",
                where,
                "a reproduction keeps the source identity_hash",
            )
        if take.seed != source.seed:
            _issue(issues, "recovery.reproduction", where, "a reproduction keeps the source seed")
        exact = (
            take.task_fingerprint == source.task_fingerprint
            and take.task_fingerprint.complete
            and source.task_fingerprint.complete
        )
        if take.exact is not exact:
            _issue(
                issues,
                "recovery.reproduction_exact",
                where,
                "'exact' must be true exactly when both task fingerprints are complete and equal",
            )
    elif take.origin is TakeOrigin.CREATIVE_REGENERATION:
        if take.identity_hash == source.identity_hash:
            _issue(
                issues,
                "recovery.regeneration",
                where,
                "a creative regeneration must differ in identity_hash",
            )
    elif take.origin is TakeOrigin.RESOURCE_ADAPTATION:
        if take.seed != source.seed:
            _issue(
                issues, "recovery.adaptation", where, "a resource adaptation keeps the source seed"
            )


def _check_asset_refs(
    issues: list[ValidationIssue],
    where: str,
    refs: Iterable[Any],
    assets: Mapping[Any, Asset],
    versions: Mapping[Any, AssetVersion],
) -> None:
    for ref in refs:
        path = f"{where}.references[{ref.role}]"
        if ref.asset_id not in assets:
            _issue(issues, "ref.missing", path, f"unknown asset {ref.asset_id}")
        if ref.version_id is None:
            continue
        version = versions.get(ref.version_id)
        if version is None:
            _issue(issues, "ref.missing", path, f"unknown version {ref.version_id}")
            continue
        if version.asset_id != ref.asset_id:
            _issue(issues, "ref.mismatch", path, "version belongs to another asset")
        if ref.sha256 is not None and ref.sha256 != version.object_ref.sha256:
            _issue(
                issues, "ref.hash_mismatch", path, "content hash differs from the pinned version"
            )


# ---------------------------------------------------------------- assets


def _check_assets(s: DomainSnapshot, issues: list[ValidationIssue]) -> None:
    assets = {x.asset_id: x for x in s.assets}
    versions = {x.version_id: x for x in s.asset_versions}
    jobs = {x.job_id: x for x in s.jobs}
    attempts = {x.attempt_id: x for x in s.attempts}

    for asset in s.assets:
        if asset.project_id != s.project.project_id:
            _issue(issues, "ref.project", f"assets[{asset.asset_id}]", "belongs to another project")
    parents_of: dict[str, int] = defaultdict(int)
    for edge in s.lineage:
        parents_of[edge.child_version_id] += 1
        for end, label in ((edge.parent_version_id, "parent"), (edge.child_version_id, "child")):
            if end not in versions:
                _issue(
                    issues,
                    "ref.missing",
                    f"lineage[{edge.parent_version_id}->{edge.child_version_id}]",
                    f"unknown {label} version {end}",
                )
    issues.extend(validate_lineage(s.lineage))

    for version in s.asset_versions:
        where = f"asset_versions[{version.version_id}]"
        asset = assets.get(version.asset_id)
        if asset is None:
            _issue(issues, "ref.missing", f"{where}.asset_id", f"unknown asset {version.asset_id}")
        else:
            for found in check_metadata_for(
                asset.media_type, version.metadata, f"{where}.metadata"
            ):
                issues.append(found)
        prov = version.provenance
        if prov.job_id is not None and prov.job_id not in jobs:
            _issue(
                issues, "ref.missing", f"{where}.provenance.job_id", f"unknown job {prov.job_id}"
            )
        if prov.attempt_id is not None:
            attempt = attempts.get(prov.attempt_id)
            if attempt is None:
                _issue(
                    issues,
                    "ref.missing",
                    f"{where}.provenance.attempt_id",
                    f"unknown attempt {prov.attempt_id}",
                )
            elif attempt.job_id != prov.job_id:
                _issue(
                    issues,
                    "ref.mismatch",
                    f"{where}.provenance.attempt_id",
                    "attempt belongs to another job",
                )
        if prov.origin is ProvenanceOrigin.DERIVED and parents_of[version.version_id] == 0:
            _issue(
                issues,
                "lineage.derived_orphan",
                where,
                "a derived version needs at least one lineage parent",
            )
        if prov.origin is ProvenanceOrigin.IMPORTED and parents_of[version.version_id] > 0:
            _issue(
                issues,
                "lineage.imported_parent",
                where,
                "an imported version has no lineage parents",
            )


# ---------------------------------------------------------------- workflow


def _check_workflow(s: DomainSnapshot, issues: list[ValidationIssue]) -> None:
    jobs = {x.job_id: x for x in s.jobs}
    attempts = {x.attempt_id: x for x in s.attempts}
    gates = {x.gate_id: x for x in s.gates}
    takes = {x.take_id: x for x in s.takes}
    versions = {x.version_id: x for x in s.asset_versions}
    approvals = {x.approval_id: x for x in s.approvals}
    attempts_by_job: dict[str, list[Attempt]] = defaultdict(list)
    for attempt in s.attempts:
        attempts_by_job[attempt.job_id].append(attempt)

    jobs_by_take: dict[str, list[Job]] = defaultdict(list)
    for job in s.jobs:
        where = f"jobs[{job.job_id}]"
        if job.project_id != s.project.project_id:
            _issue(issues, "ref.project", where, "belongs to another project")
        if job.take_id is not None:
            jobs_by_take[job.take_id].append(job)
            take = takes.get(job.take_id)
            if take is None:
                _issue(issues, "ref.missing", f"{where}.take_id", f"unknown take {job.take_id}")
            else:
                if job.identity_hash is not None and job.identity_hash != take.identity_hash:
                    _issue(
                        issues, "ref.hash_mismatch", where, "job and take disagree on identity_hash"
                    )
                if (
                    job.task_fingerprint is not None
                    and job.task_fingerprint != take.task_fingerprint
                ):
                    _issue(
                        issues,
                        "ref.hash_mismatch",
                        where,
                        "job and take disagree on task_fingerprint",
                    )
                seed = job.task.creative.seed
                if seed is not None and seed != take.seed:
                    _issue(issues, "ref.seed_mismatch", where, "job and take disagree on seed")
        for ref in job.task.creative.references:
            if ref.version_id is not None and ref.version_id not in versions:
                _issue(
                    issues,
                    "ref.missing",
                    f"{where}.task.references[{ref.role}]",
                    f"unknown version {ref.version_id}",
                )
        _check_job_attempts(
            issues, where, job, sorted(attempts_by_job.get(job.job_id, ()), key=lambda a: a.number)
        )
    for take_id, owned in sorted(jobs_by_take.items()):
        if len(owned) > 1:
            _issue(
                issues,
                "ref.take_jobs",
                f"takes[{take_id}]",
                "a take is executed by at most one job",
            )

    for attempt in s.attempts:
        where = f"attempts[{attempt.attempt_id}]"
        if attempt.job_id not in jobs:
            _issue(issues, "ref.missing", f"{where}.job_id", f"unknown job {attempt.job_id}")
        for version_id in attempt.output_version_ids:
            if version_id not in versions:
                _issue(
                    issues,
                    "ref.missing",
                    f"{where}.output_version_ids",
                    f"unknown version {version_id}",
                )
        for version_id in attempt.artifact_version_ids:
            if version_id not in versions:
                _issue(
                    issues,
                    "ref.missing",
                    f"{where}.artifact_version_ids",
                    f"unknown version {version_id}",
                )
        if (
            attempt.resume_artifact_version_id is not None
            and attempt.resume_artifact_version_id not in versions
        ):
            _issue(issues, "ref.missing", f"{where}.resume_artifact_version_id", "unknown version")
        if attempt.previous_attempt_id is not None:
            previous = attempts.get(attempt.previous_attempt_id)
            if previous is None:
                _issue(issues, "ref.missing", f"{where}.previous_attempt_id", "unknown attempt")
            elif previous.job_id != attempt.job_id or previous.number != attempt.number - 1:
                _issue(
                    issues,
                    "ref.mismatch",
                    f"{where}.previous_attempt_id",
                    "must be number n-1 of the same job",
                )
            else:
                kind = ATTEMPT_ORIGIN_KIND[attempt.origin]
                if kind is not None and not is_recovery_permitted(
                    previous.state, previous.error_class, kind
                ):
                    _issue(
                        issues, "recovery.not_permitted", where,
                        f"{attempt.origin} cannot follow an attempt that is {previous.state}"
                        f" with error class {previous.error_class}",
                    )  # fmt: skip

    graph_nodes = tuple(NodeRef(kind=NodeKind.JOB, node_id=j) for j in jobs) + tuple(
        NodeRef(kind=NodeKind.GATE, node_id=g) for g in gates
    )
    issues.extend(WorkflowGraph(nodes=graph_nodes, dependencies=s.dependencies).validate())

    for gate in s.gates:
        where = f"gates[{gate.gate_id}]"
        if gate.project_id != s.project.project_id:
            _issue(issues, "ref.project", where, "belongs to another project")
        if gate.approval_id is None:
            continue
        approval = approvals.get(gate.approval_id)
        if approval is None:
            _issue(
                issues,
                "ref.missing",
                f"{where}.approval_id",
                f"unknown approval {gate.approval_id}",
            )
            continue
        if (
            approval.purpose is not ApprovalPurpose.GATE_DECISION
            or approval.subject.subject_id != gate.gate_id
        ):
            _issue(
                issues,
                "ref.mismatch",
                f"{where}.approval_id",
                "approval is not this gate's decision",
            )
        expected = {
            GateState.PASSED: ApprovalState.APPROVED,
            GateState.REJECTED: ApprovalState.REJECTED,
        }.get(gate.state)
        if expected is not None and approval.state is not expected:
            _issue(
                issues,
                "state.gate",
                where,
                f"{gate.state} gate needs an {expected} decision, found {approval.state}",
            )

    for approval in s.approvals:
        where = f"approvals[{approval.approval_id}]"
        subject = approval.subject
        exists: dict[ApprovalSubjectKind, Callable[[str], bool]] = {
            ApprovalSubjectKind.TAKE: lambda i: i in takes,
            ApprovalSubjectKind.ASSET_VERSION: lambda i: i in versions,
            ApprovalSubjectKind.GATE: lambda i: i in gates,
        }
        if not exists[subject.kind](subject.subject_id):
            _issue(
                issues,
                "ref.missing",
                f"{where}.subject",
                f"unknown {subject.kind} {subject.subject_id}",
            )


def _check_job_attempts(
    issues: list[ValidationIssue], where: str, job: Job, ordered: list[Attempt]
) -> None:
    live = [a for a in ordered if not ATTEMPT_TRANSITIONS.is_terminal(a.state)]
    if len(live) > 1:
        _issue(issues, "state.attempts", where, "more than one non-terminal attempt")
    if live and ordered[-1] is not live[0]:
        _issue(issues, "state.attempts", where, "a non-terminal attempt must be the latest one")
    last = ordered[-1] if ordered else None

    if job.state is JobState.FAILED:
        attempt_failed = last is not None and last.state in (
            AttemptState.FAILED,
            AttemptState.INTERRUPTED,
        )
        if not attempt_failed and job.failure is None:
            _issue(
                issues,
                "state.job_failure",
                where,
                "a failed job whose latest attempt did not fail or get interrupted needs a failure record",
            )
        if attempt_failed and job.failure is not None:
            _issue(
                issues,
                "state.job_failure",
                where,
                "a failure record is only for a job that failed without a failing attempt",
            )

    if job.state in (JobState.QUEUED, JobState.BLOCKED) and ordered:
        _issue(issues, "state.job_attempts", where, f"a {job.state} job has no attempts")
    if JOB_TRANSITIONS.is_terminal(job.state) and live:
        _issue(issues, "state.job_attempts", where, f"a {job.state} job has a non-terminal attempt")
    if last is not None:
        for attempt_state, job_state in ACTIVE_JOB_STATE_FOR_ATTEMPT.items():
            if last.state is attempt_state and job.state is not job_state:
                _issue(
                    issues,
                    "state.job_attempts",
                    where,
                    f"latest attempt is {attempt_state} but job is {job.state}",
                )
            if job.state is job_state and last.state is not attempt_state:
                _issue(
                    issues,
                    "state.job_attempts",
                    where,
                    f"job is {job.state} but latest attempt is {last.state}",
                )
    if job.state is JobState.SUCCEEDED and (
        last is None or last.state is not AttemptState.SUCCEEDED
    ):
        _issue(issues, "state.job_attempts", where, "a succeeded job ends with a succeeded attempt")
    if job.state is JobState.RETRY_PENDING and (
        last is None or last.state not in (AttemptState.FAILED, AttemptState.INTERRUPTED)
    ):
        _issue(
            issues,
            "state.job_attempts",
            where,
            "retry pending needs a failed or interrupted latest attempt",
        )
    if job.state in (JobState.RUNNING, JobState.INGESTING) and last is None:
        _issue(issues, "state.job_attempts", where, f"a {job.state} job needs an attempt")


# ---------------------------------------------------------------- recovery policy (UD-4, UD-7)


def _check_recovery(s: DomainSnapshot, issues: list[ValidationIssue]) -> None:
    """Derived-counter checks against the policy snapshot on each job. Nothing here assumes a default limit:
    a limit of ``None`` means the kind is never automatic, so no automatic attempt of it may exist."""
    versions = {x.version_id: x for x in s.asset_versions}
    attempts_by_job: dict[str, list[Attempt]] = defaultdict(list)
    for attempt in s.attempts:
        attempts_by_job[attempt.job_id].append(attempt)

    for job in s.jobs:
        where = f"jobs[{job.job_id}]"
        ordered = sorted(attempts_by_job.get(job.job_id, ()), key=lambda a: a.number)
        by_id = {a.attempt_id: a for a in ordered}
        policy = job.recovery_policy
        allowed_retries = (
            0 if policy.max_infrastructure_retries is None else policy.max_infrastructure_retries
        )
        allowed_resumes = 0 if policy.max_resumes is None else policy.max_resumes
        allowed_depth = 1 if policy.max_resume_depth is None else policy.max_resume_depth
        if retries_used(ordered) > allowed_retries:
            _issue(
                issues,
                "recovery.retry_budget",
                where,
                f"{retries_used(ordered)} budget-consuming retries exceed the limit {allowed_retries}",
            )
        if resumes_used(ordered) > allowed_resumes:
            _issue(
                issues,
                "recovery.resume_budget",
                where,
                f"{resumes_used(ordered)} budget-consuming resumes exceed the limit {allowed_resumes}",
            )

        for attempt in ordered:
            path = f"attempts[{attempt.attempt_id}]"
            previous = (
                None
                if attempt.previous_attempt_id is None
                else by_id.get(attempt.previous_attempt_id)
            )
            if attempt.origin in (AttemptOrigin.INFRASTRUCTURE_RETRY, AttemptOrigin.RESUME):
                # R-11: R-2 takes precedence over R-3. A user stop forbids every automatic follow-up, even
                # when the stopped attempt never started; only a human MANUAL_RESTART may follow it.
                if previous is not None and previous.cause is AttemptEndCause.USER_STOP:
                    _issue(
                        issues,
                        "recovery.after_user_stop",
                        path,
                        "a user stop is never followed by an automatic retry or resume",
                    )
                else:
                    # R-3 (below R-2): the re-dispatch after an attempt that never started is an AUTOMATIC
                    # attempt with origin INFRASTRUCTURE_RETRY. It is not recovery: no policy limit, no budget.
                    redispatch = (
                        attempt.origin is AttemptOrigin.INFRASTRUCTURE_RETRY
                        and previous is not None
                        and never_started(previous)
                    )
                    if not redispatch and not policy.permits(attempt.origin):
                        _issue(
                            issues,
                            "recovery.not_permitted_by_policy",
                            path,
                            f"the policy ({policy.source}) sets no limit for {attempt.origin}",
                        )
            if (
                attempt.origin is AttemptOrigin.MANUAL_RESTART
                and previous is not None
                and previous.state
                not in (AttemptState.FAILED, AttemptState.INTERRUPTED, AttemptState.CANCELED)
            ):
                _issue(
                    issues,
                    "recovery.manual_restart",
                    path,
                    f"a manual restart cannot follow an attempt that is {previous.state}",
                )
            if attempt.origin is AttemptOrigin.RESUME:
                _check_resume(issues, path, job, attempt, ordered, versions, allowed_depth)


def _check_resume(
    issues: list[ValidationIssue],
    path: str,
    job: Job,
    attempt: Attempt,
    ordered: list[Attempt],
    versions: Mapping[Any, AssetVersion],
    allowed_depth: int,
) -> None:
    """A RESUME attempt reuses a stage artifact of the same job, pinned by hash, for the same task
    fingerprint and stage, within the chain-depth limit."""
    if resume_chain_depth(attempt, ordered) > allowed_depth:
        _issue(issues, "recovery.resume_depth", path, f"resume chain longer than {allowed_depth}")
    artifact_id = attempt.resume_artifact_version_id
    earlier = {v for e in ordered if e.number < attempt.number for v in e.artifact_version_ids}
    if artifact_id not in earlier:
        _issue(
            issues,
            "recovery.resume_artifact",
            path,
            "the artifact is not listed by an earlier attempt of this job",
        )
    version = versions.get(artifact_id)
    if version is None:
        return
    if version.state is not AssetState.AVAILABLE:
        _issue(issues, "recovery.resume_artifact", path, f"the artifact is {version.state}")
    if version.object_ref.sha256 != attempt.resume_artifact_sha256:
        _issue(
            issues, "recovery.resume_hash", path, "the artifact's bytes differ from the pinned hash"
        )
    provenance = version.provenance
    task_fingerprint = job.task_fingerprint
    if (
        task_fingerprint is None
        or not task_fingerprint.complete
        or provenance.stage is None
        or provenance.stage_key != stage_key(task_fingerprint, provenance.stage)
    ):
        _issue(
            issues,
            "recovery.resume_stage_key",
            path,
            "the artifact's stage key does not match this job's complete task fingerprint",
        )


# ---------------------------------------------------------------- production <-> workflow


def _check_cross(s: DomainSnapshot, issues: list[ValidationIssue]) -> None:
    jobs_by_take = {j.take_id: j for j in s.jobs if j.take_id is not None}
    attempts_by_job: dict[str, list[Attempt]] = defaultdict(list)
    for attempt in s.attempts:
        attempts_by_job[attempt.job_id].append(attempt)
    approvals_for_take: dict[str, list[Approval]] = defaultdict(list)
    for approval in s.approvals:
        if (
            approval.subject.kind is ApprovalSubjectKind.TAKE
            and approval.purpose is ApprovalPurpose.CONTENT_REVIEW
        ):
            approvals_for_take[approval.subject.subject_id].append(approval)

    for take in s.takes:
        where = f"takes[{take.take_id}]"
        job = jobs_by_take.get(take.take_id)
        if take.state is TakeState.GENERATING and (
            job is None or JOB_TRANSITIONS.is_terminal(job.state)
        ):
            _issue(issues, "state.take_job", where, "a generating take needs a non-terminal job")
        if take.state in (TakeState.PRODUCED, TakeState.APPROVED):
            if job is None or job.state is not JobState.SUCCEEDED:
                _issue(
                    issues, "state.take_job", where, f"a {take.state} take needs a succeeded job"
                )
            else:
                produced = {
                    v
                    for a in attempts_by_job.get(job.job_id, ())
                    if a.state is AttemptState.SUCCEEDED
                    for v in a.output_version_ids
                }
                extra = set(take.output_version_ids) - produced
                if extra:
                    _issue(
                        issues,
                        "state.take_outputs",
                        where,
                        f"outputs not produced by its job: {sorted(extra)}",
                    )
        if take.state is TakeState.APPROVED and not any(
            a.state is ApprovalState.APPROVED for a in approvals_for_take.get(take.take_id, ())
        ):
            _issue(
                issues,
                "state.take_approval",
                where,
                "an approved take needs an approved content review",
            )
