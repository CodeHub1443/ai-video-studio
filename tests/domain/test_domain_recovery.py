"""Recovery policy (UD-4, UD-7 amended): causes, derived counters, policy checks, resume rules, Job.failure."""

from __future__ import annotations

from dataclasses import MISSING, fields
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from studio.domain.assets import Asset, AssetVersion, ObjectRef, Provenance, ProvenanceOrigin
from studio.domain.errors import DomainValidationError, InvalidTransition
from studio.domain.fingerprint import Fingerprint, FingerprintKind, stage_key
from studio.domain.production import CreativeSpec, Project
from studio.domain.recovery import (
    ATTEMPT_ORIGIN_KIND,
    RECOVERY_RULES,
    AdmissionRefusal,
    AttemptEndCause,
    AttemptInitiator,
    AttemptOrigin,
    BudgetKind,
    ErrorClass,
    JobFailure,
    RecoveryKind,
    RecoveryPolicySnapshot,
    consumes_budget,
    counts_toward_breaker,
    is_recovery_permitted,
    never_started,
    permitted_take_recovery,
    resume_chain_depth,
    resumes_used,
    retries_used,
)
from studio.domain.states import AssetState, AttemptState, JobState
from studio.domain.validation import DomainSnapshot, validate_snapshot
from studio.domain.vocab import MediaType, Operation
from studio.domain.workflow import Attempt, Job, TaskSpec, check_job_transition

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
SHA_A, SHA_B = "11" * 32, "22" * 32
IDENT = Fingerprint(kind=FingerprintKind.CREATIVE_IDENTITY, digest="aa" * 32)
TASKFP = Fingerprint(kind=FingerprintKind.TASK_FINGERPRINT, digest="bb" * 32)
INCOMPLETE_TASKFP = Fingerprint(
    kind=FingerprintKind.TASK_FINGERPRINT, digest="bb" * 32, complete=False
)

ENGINE, STUDIO, HOST, USER = (
    AttemptEndCause.ENGINE,
    AttemptEndCause.STUDIO_PROCESS,
    AttemptEndCause.HOST_RESTART,
    AttemptEndCause.USER_STOP,
)
RETRY, RESUME, MANUAL = (
    AttemptOrigin.INFRASTRUCTURE_RETRY,
    AttemptOrigin.RESUME,
    AttemptOrigin.MANUAL_RESTART,
)


def _code(excinfo: pytest.ExceptionInfo[DomainValidationError]) -> str:
    return excinfo.value.issues[0].code


def policy(
    retries: int | None = None,
    resumes: int | None = None,
    depth: int | None = None,
    source: str = "binding:cap.t2i",
) -> RecoveryPolicySnapshot:
    return RecoveryPolicySnapshot(
        source=source,
        max_infrastructure_retries=retries,
        max_resumes=resumes,
        max_resume_depth=depth,
    )


def att(
    number: int,
    state: AttemptState,
    *,
    origin: AttemptOrigin = AttemptOrigin.INITIAL,
    cause: AttemptEndCause | None = None,
    started: bool = True,
    **kw: Any,
) -> Attempt:
    """A consistent attempt row: attempt ``number`` follows attempt ``number - 1`` of job_1."""
    base: dict[str, Any] = {
        "attempt_id": f"att_{number}",
        "job_id": "job_1",
        "number": number,
        "state": state,
        "origin": origin,
        "initiated_by": AttemptInitiator.HUMAN if origin is MANUAL else AttemptInitiator.AUTOMATIC,
        "initiated_by_name": "ada" if origin is MANUAL else None,
        "previous_attempt_id": None if number == 1 else f"att_{number - 1}",
    }
    if origin is RESUME:
        base["resume_artifact_version_id"] = "art_1"
        base["resume_artifact_sha256"] = SHA_A
    if started and state is not AttemptState.PENDING:
        base["started_at"] = T0 + timedelta(minutes=number)
    if cause is not None:
        base["cause"] = cause
    if state in (AttemptState.FAILED, AttemptState.INTERRUPTED):
        base["finished_at"] = T0 + timedelta(minutes=number, seconds=30)
    if state is AttemptState.FAILED:
        base["error_class"] = ErrorClass.INFRASTRUCTURE
    if state is AttemptState.SUCCEEDED:
        base["output_version_ids"] = ("out_1",)
        base["finished_at"] = T0 + timedelta(minutes=number, seconds=30)
    base.update(kw)
    return Attempt(**base)


def failed(number: int, **kw: Any) -> Attempt:
    return att(number, AttemptState.FAILED, cause=ENGINE, **kw)


def interrupted(number: int, cause: AttemptEndCause, **kw: Any) -> Attempt:
    return att(number, AttemptState.INTERRUPTED, cause=cause, **kw)


# ---------------------------------------------------------------- the policy snapshot


def test_policy_fields_have_no_default() -> None:
    init_fields = [f for f in fields(RecoveryPolicySnapshot) if f.init]
    assert {f.name for f in init_fields} == {
        "source",
        "max_infrastructure_retries",
        "max_resumes",
        "max_resume_depth",
    }
    for f in init_fields:
        assert f.default is MISSING and f.default_factory is MISSING, f.name
    nothing: dict[str, Any] = {}
    with pytest.raises(TypeError):
        RecoveryPolicySnapshot(**nothing)


def test_job_requires_a_recovery_policy() -> None:
    names = {f.name: f for f in fields(Job)}
    assert "max_infrastructure_retries" not in names
    assert names["recovery_policy"].default is MISSING
    without_policy: dict[str, Any] = {
        "job_id": "job_1",
        "project_id": "proj_1",
        "task": TaskSpec(creative=_creative()),
    }
    with pytest.raises(TypeError):
        Job(**without_policy)


def test_policy_limits_validation() -> None:
    for bad in (-1, True, 1.5, "3"):
        raw: dict[str, Any] = {
            "source": "binding:cap.t2i",
            "max_infrastructure_retries": bad,
            "max_resumes": None,
            "max_resume_depth": None,
        }
        with pytest.raises(DomainValidationError) as err:
            RecoveryPolicySnapshot(**raw)
        assert _code(err) == "policy.limit"
    assert policy(retries=0).max_infrastructure_retries == 0
    assert policy().permits(RETRY) is False


def test_policy_source_rules() -> None:
    none = RecoveryPolicySnapshot.no_automatic_recovery()
    assert none.source == "none"
    assert (none.max_infrastructure_retries, none.max_resumes, none.max_resume_depth) == (
        None,
        None,
        None,
    )
    with pytest.raises(DomainValidationError) as err:
        policy(retries=1, source="none")
    assert _code(err) == "policy.source_none"
    with pytest.raises(DomainValidationError) as err2:
        policy(source="registry:cap.t2i")
    assert _code(err2) == "policy.source"
    with pytest.raises(DomainValidationError):
        policy(source="binding:Bad Id")
    assert policy(source="engine:ltx_streaming").source == "engine:ltx_streaming"


def test_policy_digest_is_a_recovery_policy_fingerprint() -> None:
    a = policy(retries=1, resumes=1, depth=1)
    assert a.digest.kind is FingerprintKind.RECOVERY_POLICY
    assert a.digest == policy(retries=1, resumes=1, depth=1).digest
    variants = [
        policy(retries=3, resumes=1, depth=1),
        policy(retries=1, resumes=0, depth=1),
        policy(retries=1, resumes=1, depth=None),
        policy(retries=1, resumes=1, depth=1, source="engine:ltx_streaming"),
    ]
    assert len({a.digest, *(v.digest for v in variants)}) == len(variants) + 1


def test_policy_permits_follows_the_separate_limits() -> None:
    p = policy(retries=0, resumes=None)
    assert p.permits(RETRY) is True  # 0 is a limit: allowed, but nothing may consume it
    assert p.permits(RESUME) is False  # None: never automatic
    assert p.permits(AttemptOrigin.INITIAL) is False
    assert p.permits(MANUAL) is False
    assert p.limit_for(BudgetKind.RETRY) == 0
    assert p.limit_for(BudgetKind.RESUME) is None


# ---------------------------------------------------------------- attempt rules


def test_cause_rules() -> None:
    with pytest.raises(DomainValidationError) as err:
        att(1, AttemptState.FAILED, cause=None)
    assert _code(err) == "attempt.cause"
    with pytest.raises(DomainValidationError) as err2:
        att(1, AttemptState.FAILED, cause=STUDIO)
    assert _code(err2) == "attempt.cause"
    with pytest.raises(DomainValidationError) as err3:
        att(1, AttemptState.RUNNING, cause=ENGINE)
    assert _code(err3) == "attempt.cause"
    with pytest.raises(DomainValidationError) as err4:
        att(1, AttemptState.INTERRUPTED, cause=None)
    assert _code(err4) == "attempt.cause"
    assert interrupted(1, USER).cause is USER


def test_interrupted_attempt_records_its_time_and_engine_cause_needs_a_start() -> None:
    with pytest.raises(DomainValidationError) as err:
        interrupted(1, STUDIO, finished_at=None)
    assert _code(err) == "attempt.interrupted_time"
    with pytest.raises(DomainValidationError) as err2:
        interrupted(1, ENGINE, started=False)
    assert _code(err2) == "attempt.cause_unstarted"
    assert interrupted(1, STUDIO, started=False).started_at is None
    assert interrupted(1, ENGINE).cause is ENGINE


@pytest.mark.parametrize(
    "state",
    [AttemptState.RUNNING, AttemptState.INGESTING, AttemptState.SUCCEEDED, AttemptState.FAILED],
)
def test_started_at_is_required_once_the_engine_ran(state: AttemptState) -> None:
    """B-11 D2: a state that implies the engine ran cannot lack its start time, or a crashed running attempt
    would look like one that never started and escape the breaker and the budget."""
    cause = ENGINE if state is AttemptState.FAILED else None
    with pytest.raises(DomainValidationError) as err:
        att(1, state, cause=cause, started=False)
    assert _code(err) == "attempt.started_required"
    assert att(1, state, cause=cause).started_at is not None


def test_a_crashed_running_attempt_without_a_start_time_cannot_exist() -> None:
    with pytest.raises(DomainValidationError) as err:
        att(1, AttemptState.RUNNING, started=False)
    assert _code(err) == "attempt.started_required"
    with pytest.raises(DomainValidationError) as err2:
        att(1, AttemptState.INGESTING, started=False)
    assert _code(err2) == "attempt.started_required"


def test_a_pending_attempt_has_not_started() -> None:
    assert att(1, AttemptState.PENDING).started_at is None
    with pytest.raises(DomainValidationError) as err:
        att(1, AttemptState.PENDING, started_at=T0)
    assert _code(err) == "attempt.started_forbidden"


@pytest.mark.parametrize("started", [True, False])
def test_started_at_is_optional_when_interrupted_or_canceled(started: bool) -> None:
    for state, cause in ((AttemptState.INTERRUPTED, STUDIO), (AttemptState.CANCELED, None)):
        attempt = att(1, state, cause=cause, started=started)
        assert (attempt.started_at is not None) is started


def test_initiator_rules() -> None:
    manual = att(2, AttemptState.PENDING, origin=MANUAL)
    assert manual.initiated_by_name == "ada"
    bad_cases: list[dict[str, Any]] = [
        {"origin": MANUAL, "initiated_by": AttemptInitiator.AUTOMATIC},
        {"origin": RETRY, "initiated_by": AttemptInitiator.HUMAN, "initiated_by_name": "ada"},
        {"origin": MANUAL, "initiated_by_name": None},
        {"origin": RETRY, "initiated_by_name": "ada"},
    ]
    for override in bad_cases:
        kwargs: dict[str, Any] = {
            "attempt_id": "att_2",
            "job_id": "job_1",
            "number": 2,
            "previous_attempt_id": "att_1",
            "initiated_by": AttemptInitiator.HUMAN
            if override["origin"] is MANUAL
            else AttemptInitiator.AUTOMATIC,
            "initiated_by_name": "ada" if override["origin"] is MANUAL else None,
        }
        kwargs.update(override)
        with pytest.raises(DomainValidationError) as err:
            Attempt(**kwargs)
        assert _code(err) == "attempt.initiator"


def test_manual_restart_follows_an_earlier_attempt() -> None:
    with pytest.raises(DomainValidationError) as err:
        att(1, AttemptState.PENDING, origin=MANUAL, previous_attempt_id=None)
    assert _code(err) == "attempt.origin"


def test_artifact_rules() -> None:
    assert failed(1, artifact_version_ids=("art_1",)).artifact_version_ids == ("art_1",)
    assert att(1, AttemptState.CANCELED, artifact_version_ids=("art_1",)).state is (
        AttemptState.CANCELED
    )
    with pytest.raises(DomainValidationError) as err:
        att(1, AttemptState.RUNNING, artifact_version_ids=("art_1",))
    assert _code(err) == "attempt.artifacts"
    with pytest.raises(DomainValidationError) as err2:
        att(
            1,
            AttemptState.SUCCEEDED,
            output_version_ids=("out_1",),
            artifact_version_ids=("out_1",),
        )
    assert _code(err2) == "attempt.artifacts"


def test_resume_needs_the_artifact_and_its_hash() -> None:
    with pytest.raises(DomainValidationError) as err:
        att(2, AttemptState.PENDING, origin=RESUME, resume_artifact_sha256=None)
    assert _code(err) == "attempt.resume"
    with pytest.raises(DomainValidationError) as err2:
        att(2, AttemptState.PENDING, origin=RETRY, resume_artifact_sha256=SHA_A)
    assert _code(err2) == "attempt.resume"
    with pytest.raises(DomainValidationError):
        att(2, AttemptState.PENDING, origin=RESUME, resume_artifact_sha256="short")


def test_boot_id_only_on_interrupted_attempts() -> None:
    assert interrupted(1, HOST, interrupted_boot_id="boot_1").interrupted_boot_id == "boot_1"
    with pytest.raises(DomainValidationError) as err:
        failed(1, interrupted_boot_id="boot_1")
    assert _code(err) == "attempt.boot"


# ---------------------------------------------------------------- derived facts


@pytest.mark.parametrize(
    ("state", "cause", "started", "consumes", "counts"),
    [
        (AttemptState.FAILED, ENGINE, True, True, False),
        (AttemptState.INTERRUPTED, ENGINE, True, True, False),
        (AttemptState.INTERRUPTED, STUDIO, True, False, True),
        (AttemptState.INTERRUPTED, HOST, True, False, True),
        (AttemptState.INTERRUPTED, USER, True, False, False),
        (AttemptState.INTERRUPTED, STUDIO, False, False, False),
        (AttemptState.INTERRUPTED, HOST, False, False, False),
        (AttemptState.INTERRUPTED, USER, False, False, False),
    ],
)
def test_budget_and_breaker_truth_table(
    state: AttemptState, cause: AttemptEndCause, started: bool, consumes: bool, counts: bool
) -> None:
    attempt = att(1, state, cause=cause, started=started)
    assert consumes_budget(attempt) is consumes
    assert counts_toward_breaker(attempt) is counts
    assert never_started(attempt) is (state is AttemptState.INTERRUPTED and not started)


def test_non_terminal_and_clean_attempts_consume_nothing() -> None:
    for state in (AttemptState.PENDING, AttemptState.RUNNING, AttemptState.INGESTING):
        attempt = att(1, state)
        assert not consumes_budget(attempt)
        assert not counts_toward_breaker(attempt)
    assert not consumes_budget(att(1, AttemptState.SUCCEEDED))
    assert not consumes_budget(att(1, AttemptState.CANCELED))


def test_retry_counter_counts_only_followers_of_budget_consuming_attempts() -> None:
    rows = [failed(1), failed(2, origin=RETRY), att(3, AttemptState.RUNNING, origin=RETRY)]
    assert retries_used(rows) == 2
    assert resumes_used(rows) == 0
    for cause in (STUDIO, HOST, USER):
        quiet = [interrupted(1, cause), att(2, AttemptState.RUNNING, origin=RETRY)]
        assert retries_used(quiet) == 0
    unstarted = [interrupted(1, STUDIO, started=False), att(2, AttemptState.RUNNING, origin=RETRY)]
    assert retries_used(unstarted) == 0
    engine_interrupt = [interrupted(1, ENGINE), att(2, AttemptState.RUNNING, origin=RETRY)]
    assert retries_used(engine_interrupt) == 1


def test_resume_counter_is_separate_from_the_retry_counter() -> None:
    rows = [failed(1), att(2, AttemptState.RUNNING, origin=RESUME)]
    assert resumes_used(rows) == 1
    assert retries_used(rows) == 0


def test_counters_are_derived_from_rows_only() -> None:
    rows = [failed(1), failed(2, origin=RETRY)]
    assert retries_used(rows) == 1
    assert retries_used(rows + [att(3, AttemptState.PENDING, origin=RETRY)]) == 2
    assert retries_used([]) == 0
    assert retries_used(iter(rows)) == 1  # any iterable of rows


def test_resume_chain_depth() -> None:
    chain = [
        failed(1),
        failed(2, origin=RESUME),
        failed(3, origin=RESUME),
        att(4, AttemptState.RUNNING, origin=RETRY),
        att(5, AttemptState.PENDING, origin=RESUME),
    ]
    assert [resume_chain_depth(a, chain) for a in chain] == [0, 1, 2, 0, 1]


# ---------------------------------------------------------------- snapshot checks


def _creative() -> CreativeSpec:
    return CreativeSpec(operation=Operation.T2I, binding_id="cap.t2i", seed=1)


def make_job(pol: RecoveryPolicySnapshot, **kw: Any) -> Job:
    base: dict[str, Any] = {
        "job_id": "job_1",
        "project_id": "proj_1",
        "task": TaskSpec(creative=_creative()),
        "state": JobState.RETRY_PENDING,
        "identity_hash": IDENT,
        "task_fingerprint": TASKFP,
        "recovery_policy": pol,
    }
    base.update(kw)
    return Job(**base)


def make_version(**kw: Any) -> AssetVersion:
    provenance_kwargs: dict[str, Any] = {
        "origin": ProvenanceOrigin.GENERATED,
        "binding_id": "cap.t2i",
        "job_id": "job_1",
        "attempt_id": "att_1",
        "identity_hash": IDENT,
        "task_fingerprint": TASKFP,
        "stage": "stage_one",
        "stage_key": stage_key(TASKFP, "stage_one"),
    }
    provenance_kwargs.update(kw.pop("provenance", {}))
    base: dict[str, Any] = {
        "version_id": "art_1",
        "asset_id": "asset_1",
        "version_number": 1,
        "state": AssetState.AVAILABLE,
        "object_ref": ObjectRef(sha256=SHA_A, size_bytes=10),
        "provenance": Provenance(**provenance_kwargs),
    }
    base.update(kw)
    return AssetVersion(**base)


def snapshot(job: Job, attempts: list[Attempt], versions: tuple[AssetVersion, ...] = ()) -> Any:
    assets: tuple[Asset, ...] = ()
    if versions:
        assets = (
            Asset(
                **{
                    "asset_id": "asset_1",
                    "project_id": "proj_1",
                    "media_type": MediaType.IMAGE,
                    "name": "stage artifacts",
                }
            ),
        )
    project_kwargs: dict[str, Any] = {"project_id": "proj_1", "slug": "p", "title": "T"}
    return DomainSnapshot(
        project=Project(**project_kwargs),
        jobs=(job,),
        attempts=tuple(attempts),
        assets=assets,
        asset_versions=versions,
    )


def recovery_codes(
    job: Job, attempts: list[Attempt], versions: tuple[AssetVersion, ...] = ()
) -> list[str]:
    issues = validate_snapshot(snapshot(job, attempts, versions))
    return [i.code for i in issues if i.code.startswith("recovery.")]


def test_retry_limit_is_enforced_per_budget_consuming_retry() -> None:
    rows = [failed(1), failed(2, origin=RETRY), att(3, AttemptState.RUNNING, origin=RETRY)]
    assert recovery_codes(make_job(policy(retries=2)), rows) == []
    assert recovery_codes(make_job(policy(retries=1)), rows) == ["recovery.retry_budget"]
    assert recovery_codes(make_job(policy(retries=7)), rows) == []


def test_unset_retry_limit_means_no_automatic_retry() -> None:
    rows = [failed(1), att(2, AttemptState.RUNNING, origin=RETRY)]
    codes = recovery_codes(make_job(policy(retries=None)), rows)
    assert sorted(codes) == ["recovery.not_permitted_by_policy", "recovery.retry_budget"]
    none_job = make_job(RecoveryPolicySnapshot.no_automatic_recovery())
    assert sorted(recovery_codes(none_job, rows)) == sorted(codes)


def test_limit_zero_allows_only_non_budget_follow_ups() -> None:
    zero = make_job(policy(retries=0))
    after_engine_failure = [failed(1), att(2, AttemptState.RUNNING, origin=RETRY)]
    assert recovery_codes(zero, after_engine_failure) == ["recovery.retry_budget"]
    for cause in (STUDIO, HOST):
        quiet = [interrupted(1, cause), att(2, AttemptState.RUNNING, origin=RETRY)]
        assert recovery_codes(zero, quiet) == []


def test_unset_limit_forbids_even_non_budget_automatic_follow_ups() -> None:
    unset = make_job(policy(retries=None))
    for cause in (STUDIO, HOST):
        quiet = [interrupted(1, cause), att(2, AttemptState.RUNNING, origin=RETRY)]
        assert recovery_codes(unset, quiet) == ["recovery.not_permitted_by_policy"]


def test_engine_side_failures_and_interruptions_consume_the_budget() -> None:
    one = make_job(policy(retries=1))
    ok = [failed(1), att(2, AttemptState.RUNNING, origin=RETRY)]
    assert recovery_codes(one, ok) == []
    too_many = [interrupted(1, ENGINE), interrupted(2, ENGINE, origin=RETRY)]
    too_many.append(att(3, AttemptState.RUNNING, origin=RETRY))
    assert recovery_codes(one, too_many) == ["recovery.retry_budget"]


def test_user_stop_is_never_followed_by_an_automatic_attempt() -> None:
    generous = make_job(policy(retries=9, resumes=9, depth=9))
    after_stop = [interrupted(1, USER), att(2, AttemptState.RUNNING, origin=RETRY)]
    assert recovery_codes(generous, after_stop) == ["recovery.after_user_stop"]
    resume_after_stop = [
        interrupted(1, USER, artifact_version_ids=("art_1",)),
        att(2, AttemptState.RUNNING, origin=RESUME),
    ]
    assert "recovery.after_user_stop" in recovery_codes(
        generous, resume_after_stop, (make_version(),)
    )
    manual = [interrupted(1, USER), att(2, AttemptState.RUNNING, origin=MANUAL)]
    for candidate in (generous, make_job(RecoveryPolicySnapshot.no_automatic_recovery())):
        assert recovery_codes(candidate, manual) == []


def test_an_attempt_that_never_started_is_not_a_recovery_event() -> None:
    none = make_job(RecoveryPolicySnapshot.no_automatic_recovery())
    rows = [
        interrupted(1, STUDIO, started=False),
        att(2, AttemptState.RUNNING, origin=RETRY),
    ]
    assert recovery_codes(none, rows) == []  # no limit needed, no budget used
    assert retries_used(rows) == 0
    assert not counts_toward_breaker(rows[0])
    unset_with_resume = [
        interrupted(1, STUDIO, started=False, artifact_version_ids=("art_1",)),
        att(2, AttemptState.RUNNING, origin=RESUME),
    ]
    assert "recovery.not_permitted_by_policy" in recovery_codes(
        none, unset_with_resume, (make_version(),)
    )


def test_user_stop_before_the_start_still_forbids_an_automatic_follow_up() -> None:
    """R-11: R-2 outranks R-3. A user-stopped attempt that never started is followed only by a human restart."""
    generous = make_job(policy(retries=9, resumes=9, depth=9))
    stopped = interrupted(1, USER, started=False)
    after_retry = [stopped, att(2, AttemptState.RUNNING, origin=RETRY)]
    assert recovery_codes(generous, after_retry) == ["recovery.after_user_stop"]
    resume_rows = [
        interrupted(1, USER, started=False, artifact_version_ids=("art_1",)),
        att(2, AttemptState.RUNNING, origin=RESUME),
    ]
    assert "recovery.after_user_stop" in recovery_codes(generous, resume_rows, (make_version(),))
    after_human = [stopped, att(2, AttemptState.RUNNING, origin=MANUAL)]
    for candidate in (generous, make_job(RecoveryPolicySnapshot.no_automatic_recovery())):
        assert recovery_codes(candidate, after_human) == []


@pytest.mark.parametrize("cause", [STUDIO, HOST])
def test_redispatch_after_a_never_started_attempt_is_an_automatic_infrastructure_retry(
    cause: AttemptEndCause,
) -> None:
    """R-11: the re-dispatch is an AUTOMATIC attempt with origin INFRASTRUCTURE_RETRY, and it is not recovery."""
    none = make_job(RecoveryPolicySnapshot.no_automatic_recovery())
    first = interrupted(1, cause, started=False)
    second = att(2, AttemptState.RUNNING, origin=RETRY)
    assert second.origin is AttemptOrigin.INFRASTRUCTURE_RETRY
    assert second.initiated_by is AttemptInitiator.AUTOMATIC and second.initiated_by_name is None
    assert recovery_codes(none, [first, second]) == []  # no policy limit needed
    assert retries_used([first, second]) == 0  # no budget used
    assert not counts_toward_breaker(first)  # never counted toward the breaker
    with pytest.raises(DomainValidationError):  # the origin cannot be traded for a human one
        att(2, AttemptState.RUNNING, origin=MANUAL, initiated_by=AttemptInitiator.AUTOMATIC)


def test_manual_restart_is_never_budgeted() -> None:
    rows: list[Attempt] = [failed(1)]
    for n in range(2, 9):
        rows.append(failed(n, origin=MANUAL))
    rows.append(att(9, AttemptState.PENDING, origin=MANUAL))
    assert retries_used(rows) == 0 and resumes_used(rows) == 0
    assert recovery_codes(make_job(RecoveryPolicySnapshot.no_automatic_recovery()), rows) == []


def test_manual_restart_cannot_follow_a_success_or_a_live_attempt() -> None:
    job = make_job(policy())
    after_success = [att(1, AttemptState.SUCCEEDED), att(2, AttemptState.PENDING, origin=MANUAL)]
    assert recovery_codes(job, after_success) == ["recovery.manual_restart"]
    after_cancel = [att(1, AttemptState.CANCELED), att(2, AttemptState.PENDING, origin=MANUAL)]
    assert recovery_codes(job, after_cancel) == []


# ---------------------------------------------------------------- resume rules (design C.4)


def _resume_rows(depth: int = 1, **second: Any) -> list[Attempt]:
    rows = [failed(1, artifact_version_ids=("art_1",))]
    for n in range(2, 2 + depth):
        extra = second if n == 2 else {}
        rows.append(failed(n, origin=RESUME, **extra))
    return rows


def test_resume_with_a_valid_artifact_is_clean() -> None:
    job = make_job(policy(resumes=1))
    assert recovery_codes(job, _resume_rows(), (make_version(),)) == []


def test_resume_budget_is_separate_and_enforced() -> None:
    rows = [failed(1, artifact_version_ids=("art_1",)), failed(2, origin=RESUME)]
    rows.append(att(3, AttemptState.RUNNING, origin=RESUME))
    codes = recovery_codes(make_job(policy(resumes=1, depth=9)), rows, (make_version(),))
    assert codes == ["recovery.resume_budget"]
    assert (
        recovery_codes(make_job(policy(retries=0, resumes=2, depth=9)), rows, (make_version(),))
        == []
    )
    assert "recovery.not_permitted_by_policy" in recovery_codes(
        make_job(policy(retries=3, resumes=None)), _resume_rows(), (make_version(),)
    )


def test_resume_depth_limit() -> None:
    rows = [failed(1, artifact_version_ids=("art_1",))]
    rows += [failed(2, origin=RESUME), att(3, AttemptState.RUNNING, origin=RESUME)]
    versions = (make_version(),)
    unset_depth = make_job(policy(resumes=9, depth=None))
    assert recovery_codes(unset_depth, rows, versions) == ["recovery.resume_depth"]
    assert recovery_codes(make_job(policy(resumes=9, depth=2)), rows, versions) == []
    assert recovery_codes(unset_depth, _resume_rows(), versions) == []  # a single resume is fine


def test_resume_artifact_must_come_from_an_earlier_attempt_of_the_job() -> None:
    job = make_job(policy(resumes=1))
    rows = [failed(1), failed(2, origin=RESUME)]  # attempt 1 lists no artifact
    assert recovery_codes(job, rows, (make_version(),)) == ["recovery.resume_artifact"]
    own = [failed(1), failed(2, origin=RESUME, artifact_version_ids=("art_1",))]
    assert recovery_codes(job, own, (make_version(),)) == ["recovery.resume_artifact"]


def test_resume_artifact_must_be_available() -> None:
    job = make_job(policy(resumes=1))
    retired = make_version(state=AssetState.RETIRED)
    assert recovery_codes(job, _resume_rows(), (retired,)) == ["recovery.resume_artifact"]


def test_resume_pins_the_artifact_bytes() -> None:
    job = make_job(policy(resumes=1))
    changed = make_version(object_ref=ObjectRef(sha256=SHA_B, size_bytes=10))
    assert recovery_codes(job, _resume_rows(), (changed,)) == ["recovery.resume_hash"]


def test_resume_needs_a_matching_stage_key_and_a_complete_task_fingerprint() -> None:
    job = make_job(policy(resumes=1))
    wrong_stage = make_version(provenance={"stage_key": stage_key(TASKFP, "stage_two")})
    assert recovery_codes(job, _resume_rows(), (wrong_stage,)) == ["recovery.resume_stage_key"]
    other_task = Fingerprint(kind=FingerprintKind.TASK_FINGERPRINT, digest="cc" * 32)
    foreign = make_version(provenance={"stage_key": stage_key(other_task, "stage_one")})
    assert recovery_codes(job, _resume_rows(), (foreign,)) == ["recovery.resume_stage_key"]
    no_stage = make_version(provenance={"stage": None, "stage_key": None})
    assert recovery_codes(job, _resume_rows(), (no_stage,)) == ["recovery.resume_stage_key"]
    incomplete_job = make_job(policy(resumes=1), task_fingerprint=INCOMPLETE_TASKFP)
    incomplete_version = make_version(
        provenance={
            "task_fingerprint": INCOMPLETE_TASKFP,
            "stage_key": stage_key(INCOMPLETE_TASKFP, "stage_one"),
        }
    )
    assert recovery_codes(incomplete_job, _resume_rows(), (incomplete_version,)) == [
        "recovery.resume_stage_key"
    ]


def test_provenance_stage_fields_go_together() -> None:
    with pytest.raises(DomainValidationError) as err:
        make_version(provenance={"stage": "stage_one", "stage_key": None})
    assert _code(err) == "provenance.stage"
    with pytest.raises(DomainValidationError) as err2:
        Provenance(
            origin=ProvenanceOrigin.IMPORTED,
            source_description="disk",
            stage="stage_one",
            stage_key=stage_key(TASKFP, "stage_one"),
        )
    assert _code(err2) == "provenance.imported_producer"


# ---------------------------------------------------------------- Job.failure and admission refusal


def test_job_failure_is_only_for_failed_jobs() -> None:
    failure = JobFailure(
        error_class=ErrorClass.RESOURCE, refusal=AdmissionRefusal.EXCEEDS_CAPACITY, detail="12 GB"
    )
    assert make_job(policy(), state=JobState.FAILED, failure=failure).failure == failure
    with pytest.raises(DomainValidationError) as err:
        make_job(policy(), state=JobState.WAITING_RESOURCES, failure=failure)
    assert _code(err) == "job.failure"


def test_job_failure_structure() -> None:
    with pytest.raises(DomainValidationError) as err:
        JobFailure(
            error_class=ErrorClass.INFRASTRUCTURE,
            refusal=AdmissionRefusal.EXCEEDS_CAPACITY,
            detail="x",
        )
    assert _code(err) == "failure.refusal"
    with pytest.raises(DomainValidationError):
        JobFailure(error_class=ErrorClass.INVALID_SPEC, detail=" ")
    assert JobFailure(error_class=ErrorClass.INVALID_SPEC, detail="bad spec").refusal is None


def _state_codes(job: Job, attempts: list[Attempt]) -> list[str]:
    return [
        i.code for i in validate_snapshot(snapshot(job, attempts)) if i.code == "state.job_failure"
    ]


def test_failed_job_failure_record_is_required_exactly_without_a_failing_attempt() -> None:
    failure = JobFailure(
        error_class=ErrorClass.RESOURCE, refusal=AdmissionRefusal.EXCEEDS_CAPACITY, detail="12 GB"
    )
    pol = policy()
    refused = make_job(pol, state=JobState.FAILED, failure=failure)
    assert _state_codes(refused, []) == []
    assert _state_codes(make_job(pol, state=JobState.FAILED), []) == ["state.job_failure"]
    failing = [failed(1)]
    assert _state_codes(make_job(pol, state=JobState.FAILED), failing) == []
    assert _state_codes(refused, failing) == ["state.job_failure"]
    interrupted_rows = [interrupted(1, HOST)]
    assert _state_codes(make_job(pol, state=JobState.FAILED), interrupted_rows) == []


def test_waiting_resources_to_failed_needs_the_failure_record() -> None:
    ok = JobFailure(
        error_class=ErrorClass.RESOURCE, refusal=AdmissionRefusal.UNKNOWN_REQUIREMENT, detail="?"
    )
    check_job_transition(JobState.WAITING_RESOURCES, JobState.FAILED, failure=ok)
    wrong_class = JobFailure(error_class=ErrorClass.INVALID_SPEC, detail="x")
    no_refusal = JobFailure(error_class=ErrorClass.RESOURCE, detail="x")
    for bad in (None, wrong_class, no_refusal):
        with pytest.raises(DomainValidationError) as err:
            check_job_transition(JobState.WAITING_RESOURCES, JobState.FAILED, failure=bad)
        assert _code(err) == "job.failure_required"
    check_job_transition(JobState.RETRY_PENDING, JobState.FAILED)  # other edges need no record
    check_job_transition(JobState.INTERRUPTED, JobState.FAILED)
    with pytest.raises(InvalidTransition):
        check_job_transition(JobState.SUCCEEDED, JobState.FAILED)


@pytest.mark.parametrize(
    ("refusal", "has_take", "state", "expected"),
    [
        (
            AdmissionRefusal.EXCEEDS_CAPACITY,
            True,
            JobState.FAILED,
            {RecoveryKind.RESOURCE_ADAPTATION},
        ),
        (
            AdmissionRefusal.EXCEEDS_CAPACITY,
            False,
            JobState.FAILED,
            set(),
        ),  # I-12: no take, no path
        (AdmissionRefusal.UNKNOWN_REQUIREMENT, True, JobState.FAILED, set()),
        (AdmissionRefusal.PROFILE_INCOMPLETE, True, JobState.FAILED, set()),
        (None, True, JobState.FAILED, set()),
    ],
)
def test_permitted_take_recovery(
    refusal: AdmissionRefusal | None, has_take: bool, state: JobState, expected: set[RecoveryKind]
) -> None:
    failure = JobFailure(
        error_class=ErrorClass.RESOURCE if refusal else ErrorClass.INVALID_SPEC,
        refusal=refusal,
        detail="why",
    )
    job = make_job(policy(), state=state, failure=failure, take_id="take_1" if has_take else None)
    assert permitted_take_recovery(job) == frozenset(expected)


def test_permitted_take_recovery_needs_a_failed_job_with_a_record() -> None:
    assert permitted_take_recovery(make_job(policy(), take_id="take_1")) == frozenset()
    assert permitted_take_recovery(make_job(policy(), state=JobState.FAILED, take_id="take_1")) == (
        frozenset()
    )


# ---------------------------------------------------------------- the class rules around it


def test_rule_budgets_are_separate_kinds() -> None:
    assert RECOVERY_RULES[RecoveryKind.INFRASTRUCTURE_RETRY].budget is BudgetKind.RETRY
    assert RECOVERY_RULES[RecoveryKind.RESUME].budget is BudgetKind.RESUME
    for kind in (
        RecoveryKind.RESOURCE_ADAPTATION,
        RecoveryKind.CREATIVE_REGENERATION,
        RecoveryKind.REPRODUCTION,
    ):
        assert RECOVERY_RULES[kind].budget is None


def test_manual_restart_maps_to_no_recovery_kind() -> None:
    assert ATTEMPT_ORIGIN_KIND[MANUAL] is None
    assert ATTEMPT_ORIGIN_KIND[AttemptOrigin.INITIAL] is None


def test_class_rule_is_unchanged_by_the_policy() -> None:
    assert is_recovery_permitted(AttemptState.INTERRUPTED, None, RecoveryKind.INFRASTRUCTURE_RETRY)
    assert is_recovery_permitted(
        AttemptState.FAILED, ErrorClass.RESOURCE, RecoveryKind.RESOURCE_ADAPTATION
    )
    assert not is_recovery_permitted(AttemptState.FAILED, ErrorClass.UNKNOWN, RecoveryKind.RESUME)
    assert not is_recovery_permitted(AttemptState.SUCCEEDED, None, RecoveryKind.RESUME)
    assert not is_recovery_permitted(
        AttemptState.FAILED, ErrorClass.INFRASTRUCTURE, RecoveryKind.CREATIVE_REGENERATION
    )
