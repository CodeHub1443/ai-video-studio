"""Production identity fields: Rational fps, duration_ms, (role, index) references, identity payloads, Take."""

from __future__ import annotations

from typing import Any

import pytest

from studio.domain.assets import AssetRef, Provenance, ProvenanceOrigin
from studio.domain.canonical import SAFE_INT_MAX, canonical_json
from studio.domain.errors import DomainValidationError, UnresolvedSpecError
from studio.domain.fingerprint import EngineComponent, Fingerprint, FingerprintKind, ModelComponent
from studio.domain.ids import (
    AssetId,
    AttemptId,
    JobId,
    ProjectId,
    SceneId,
    ShotId,
    VersionId,
)
from studio.domain.production import (
    RESERVED_CREATIVE_KEYS,
    CreativeSpec,
    ExecutionSpec,
    Project,
    Rational,
    Scene,
    Shot,
    Take,
    check_parameter_partition,
    duration_ms_from_seconds,
    reserved_execution_only_keys,
)
from studio.domain.recovery import RecoveryPolicySnapshot, TakeOrigin
from studio.domain.states import JobState, TakeState
from studio.domain.validation import DomainSnapshot, validate_snapshot
from studio.domain.vocab import Operation
from studio.domain.workflow import Job, TaskSpec

SHA_A, SHA_B, SHA_C = "11" * 32, "22" * 32, "33" * 32


def _code(excinfo: pytest.ExceptionInfo[DomainValidationError]) -> str:
    return excinfo.value.issues[0].code


def creative(**kw: Any) -> CreativeSpec:
    base: dict[str, Any] = {
        "operation": Operation.T2I,
        "binding_id": "cap.t2i",
        "prompt": "a red apple",
        "width": 1024,
        "height": 1024,
        "seed": 42,
    }
    base.update(kw)
    return CreativeSpec(**base)


def ref(role: str = "reference", index: int = 1, sha: str | None = SHA_A, **kw: Any) -> AssetRef:
    tag = f"{role}_{index}"
    version = None if sha is None else VersionId(f"ver_{tag}")
    return AssetRef(
        role=role,
        asset_id=AssetId(f"asset_{tag}"),
        version_id=version,
        sha256=sha,
        index=index,
        **kw,
    )


def identity(spec: CreativeSpec, **kw: Any) -> Fingerprint:
    return TaskSpec(creative=spec).identity_fingerprint(**kw)


def fingerprint(kind: FingerprintKind, fill: str = "aa") -> Fingerprint:
    return Fingerprint(kind=kind, digest=fill * 32)


IDENT = fingerprint(FingerprintKind.CREATIVE_IDENTITY)
TASKFP = fingerprint(FingerprintKind.TASK_FINGERPRINT)


# ---------------------------------------------------------------- Rational and durations


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (24, (24, 1)),
        ("24", (24, 1)),
        ("24000/1001", (24000, 1001)),
        (24.0, (24, 1)),
        ("48/2", (24, 1)),
        (Rational(48, 2), (24, 1)),
    ],
)
def test_rational_parse(raw: Any, expected: tuple[int, int]) -> None:
    value = Rational.parse(raw)
    assert (value.num, value.den) == expected


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (29.97, "rational.float"),
        (float("nan"), "rational.float"),
        (True, "rational.type"),
        (None, "rational.type"),
        (0, "rational.range"),
        (-1, "rational.range"),
        (2**60, "rational.range"),
        ("24/0", "rational.range"),
        ("abc", "rational.format"),
        ("1.5", "rational.format"),
        ("24/1/1", "rational.format"),
    ],
)
def test_rational_rejects(raw: Any, code: str) -> None:
    with pytest.raises(DomainValidationError) as err:
        Rational.parse(raw)
    assert _code(err) == code


def test_rational_is_reduced_and_comparable() -> None:
    assert Rational(48, 2) == Rational(24, 1)
    assert Rational(24000, 1001).to_payload() == {"num": 24000, "den": 1001}
    assert str(Rational(24000, 1001)) == "24000/1001"


@pytest.mark.parametrize(
    ("raw", "millis"),
    [(5, 5000), (1.5, 1500), ("2.25", 2250), (0.001, 1), ("0.5", 500), (2.0, 2000)],
)
def test_duration_ms_exact(raw: Any, millis: int) -> None:
    assert duration_ms_from_seconds(raw) == millis


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (0.0005, "spec.duration_precision"),
        (0.1 + 0.2, "spec.duration_precision"),
        ("1.0005", "spec.duration_precision"),
        ("1e3", "spec.duration_format"),
        ("-1", "spec.duration_format"),
        ("", "spec.duration_format"),
        (0, "spec.range"),
        ("0", "spec.range"),
        (True, "spec.duration_type"),
        (float("nan"), "spec.duration_type"),
        (None, "spec.duration_type"),
    ],
)
def test_duration_ms_rejects(raw: Any, code: str) -> None:
    with pytest.raises(DomainValidationError) as err:
        duration_ms_from_seconds(raw)
    assert _code(err) == code


# ---------------------------------------------------------------- CreativeSpec


def test_fps_is_coerced_to_rational() -> None:
    assert creative(fps=24).fps == Rational(24, 1)
    assert creative(fps=24.0).fps == Rational(24, 1)
    assert creative(fps="24000/1001").fps == Rational(24000, 1001)
    with pytest.raises(DomainValidationError) as err:
        creative(fps=29.97)
    assert _code(err) == "rational.float"


@pytest.mark.parametrize(
    "kw",
    [
        {"width": 0},
        {"height": -1},
        {"width": SAFE_INT_MAX + 1},
        {"duration_ms": 0},
        {"width": True},
    ],
)
def test_numeric_fields_are_range_checked(kw: dict[str, Any]) -> None:
    with pytest.raises(DomainValidationError) as err:
        creative(**kw)
    assert _code(err) == "spec.range"


@pytest.mark.parametrize("seed", [-1, 2**64, True, 1.5])
def test_seed_range(seed: Any) -> None:
    with pytest.raises(DomainValidationError) as err:
        creative(seed=seed)
    assert _code(err) == "spec.seed"


def test_largest_seed_is_a_decimal_string_in_the_payload() -> None:
    payload = TaskSpec(creative=creative(seed=2**64 - 1)).identity_payload()
    assert payload["seed"] == "18446744073709551615"
    assert '"seed":"18446744073709551615"' in canonical_json(payload)


def test_free_parameter_range() -> None:
    assert creative(parameters={"big": SAFE_INT_MAX}).parameters["big"] == SAFE_INT_MAX
    with pytest.raises(DomainValidationError) as err:
        creative(parameters={"big": SAFE_INT_MAX + 1})
    assert _code(err) == "canonical.int_range"


@pytest.mark.parametrize("key", ["fps", "duration_ms", "duration_s", "width", "seed", "references"])
def test_reserved_keys_are_not_free_parameters(key: str) -> None:
    with pytest.raises(DomainValidationError) as err:
        creative(parameters={key: 1})
    assert _code(err) == "spec.reserved_key"


def test_is_resolved_needs_binding_seed_and_pinned_references() -> None:
    assert creative().is_resolved
    assert not creative(binding_id=None).is_resolved
    assert not creative(seed=None).is_resolved
    assert not creative(references=(ref(sha=None),)).is_resolved
    for spec in (
        creative(binding_id=None),
        creative(seed=None),
        creative(references=(ref(sha=None),)),
    ):
        with pytest.raises(UnresolvedSpecError):
            TaskSpec(creative=spec).identity_payload()


# ---------------------------------------------------------------- references


def test_reference_element_type_is_checked() -> None:
    with pytest.raises(DomainValidationError) as err:
        creative(references=({"role": "reference"},))
    assert _code(err) == "spec.reference_type"


def test_reference_role_and_index_must_be_unique() -> None:
    with pytest.raises(DomainValidationError) as err:
        creative(references=(ref(sha=SHA_A), ref(sha=SHA_B)))
    assert _code(err) == "spec.reference_duplicate"


@pytest.mark.parametrize("indices", [[2], [1, 3], [2, 3]])
def test_reference_indices_must_be_contiguous_from_one(indices: list[int]) -> None:
    with pytest.raises(DomainValidationError) as err:
        creative(references=tuple(ref(index=i) for i in indices))
    assert _code(err) == "spec.reference_index"


def test_asset_ref_index_validation() -> None:
    for bad in (0, -1, True):
        with pytest.raises(DomainValidationError) as err:
            AssetRef(role="reference", asset_id=AssetId("a"), index=bad)
        assert _code(err) == "asset_ref.index"


def test_reference_order_does_not_change_identity() -> None:
    refs = (ref("reference", 1, SHA_A), ref("reference", 2, SHA_B), ref("start_frame", 1, SHA_C))
    forward = creative(references=refs)
    backward = creative(references=tuple(reversed(refs)))
    assert identity(forward) == identity(backward)
    payload = TaskSpec(creative=backward).identity_payload()
    assert [(r["role"], r["index"]) for r in payload["references"]] == [
        ("reference", 1),
        ("reference", 2),
        ("start_frame", 1),
    ]


def test_reference_content_changes_identity_but_database_ids_do_not() -> None:
    base = identity(creative(references=(ref(sha=SHA_A),)))
    assert identity(creative(references=(ref(sha=SHA_B),))) != base
    other_ids = AssetRef(
        role="reference",
        asset_id=AssetId("other_asset"),
        version_id=VersionId("other_version"),
        sha256=SHA_A,
        index=1,
    )
    assert identity(creative(references=(other_ids,))) == base


# ---------------------------------------------------------------- identity payload


def test_identity_payload_shape() -> None:
    spec = creative(
        fps="24000/1001",
        duration_ms=2000,
        negative_prompt="blurry",
        parameters={"steps": 20},
        references=(ref(),),
    )
    assert TaskSpec(creative=spec).identity_payload() == {
        "operation": Operation.T2I,
        "capability": "cap.t2i",
        "prompt": "a red apple",
        "negative_prompt": "blurry",
        "width": 1024,
        "height": 1024,
        "fps": {"num": 24000, "den": 1001},
        "duration_ms": 2000,
        "seed": "42",
        "references": [{"role": "reference", "index": 1, "sha256": SHA_A}],
        "parameters": {"steps": 20},
    }


def test_equal_rate_spellings_share_one_identity() -> None:
    base = identity(creative(fps=24, duration_ms=2000))
    assert identity(creative(fps=24.0, duration_ms=2000)) == base
    assert identity(creative(fps="24", duration_ms=2000)) == base
    assert identity(creative(fps="48/2", duration_ms=2000)) == base
    assert identity(creative(fps="24000/1001", duration_ms=2000)) != base


def test_prompt_unicode_is_not_normalized() -> None:
    assert identity(creative(prompt="é")) != identity(creative(prompt="é"))


@pytest.mark.parametrize(
    "kw",
    [
        {"prompt": "other"},
        {"negative_prompt": "x"},
        {"seed": 43},
        {"width": 512},
        {"fps": 24},
        {"duration_ms": 1000},
        {"parameters": {"steps": 1}},
        {"binding_id": "cap.other"},
        {"operation": Operation.I2I},
    ],
)
def test_every_creative_field_changes_the_identity(kw: dict[str, Any]) -> None:
    assert identity(creative(**kw)) != identity(creative())


# ---------------------------------------------------------------- UD-6: execution-only reserved fields


def test_declared_reserved_fields_leave_the_identity() -> None:
    spec = creative(fps=24, duration_ms=1000, width=512, height=512)
    moved = {"width", "height", "duration_s"}
    payload = TaskSpec(creative=spec).identity_payload(execution_only_reserved=moved)
    assert "width" not in payload and "height" not in payload and "duration_ms" not in payload
    assert payload["fps"] == {"num": 24, "den": 1}
    assert spec.execution_reserved_values(moved) == {
        "duration_ms": 1000,
        "height": 512,
        "width": 512,
    }


def test_adaptable_fields_do_not_change_identity_when_declared() -> None:
    small = creative(width=512, height=512)
    large = creative(width=1024, height=1024)
    assert identity(small) != identity(large)
    moved = {"width", "height"}
    assert identity(small, execution_only_reserved=moved) == identity(
        large, execution_only_reserved=moved
    )


@pytest.mark.parametrize("name", ["fps", "prompt", "seed", "bogus"])
def test_only_the_allowlist_may_be_declared_execution_only(name: str) -> None:
    with pytest.raises(DomainValidationError) as err:
        TaskSpec(creative=creative()).identity_payload(execution_only_reserved={name})
    assert _code(err) == "spec.execution_only_reserved"
    with pytest.raises(DomainValidationError):
        creative().execution_reserved_values({name})


def test_reserved_execution_only_keys_ignores_ordinary_keys() -> None:
    assert reserved_execution_only_keys({"width", "SYNC_S1", "tile"}) == {"width"}
    assert reserved_execution_only_keys(()) == frozenset()


# ---------------------------------------------------------------- task fingerprint vs identity


def _task_fp(task: TaskSpec, **kw: Any) -> Fingerprint:
    return task.task_fingerprint(
        models=[ModelComponent(model_id="m.a", role="unet", sha256=SHA_A, size_bytes=10)],
        template_sha256=None,
        engine=EngineComponent(engine_id="e.a", version="1.0"),
        **kw,
    )


def test_execution_change_keeps_identity_and_changes_task_fingerprint() -> None:
    a = TaskSpec(creative=creative(), execution=ExecutionSpec(parameters={"offload": True}))
    b = TaskSpec(creative=creative(), execution=ExecutionSpec(parameters={"offload": False}))
    assert a.identity_fingerprint() == b.identity_fingerprint()
    assert _task_fp(a) != _task_fp(b)


def test_execution_payload_carries_moved_reserved_fields() -> None:
    task = TaskSpec(creative=creative(width=512), execution=ExecutionSpec(parameters={"tile": 1}))
    assert task.execution_payload(execution_only_reserved={"width"}) == {
        "parameters": {"tile": 1},
        "reserved": {"width": 512},
    }
    assert task.execution_payload() == {"parameters": {"tile": 1}, "reserved": {}}
    # An adapted resolution changes the task fingerprint but not the identity.
    adapted = TaskSpec(
        creative=creative(width=256), execution=ExecutionSpec(parameters={"tile": 1})
    )
    moved = {"width"}
    assert task.identity_fingerprint(execution_only_reserved=moved) == adapted.identity_fingerprint(
        execution_only_reserved=moved
    )
    assert _task_fp(task, execution_only_reserved=moved) != _task_fp(
        adapted, execution_only_reserved=moved
    )


@pytest.mark.parametrize("key", sorted(RESERVED_CREATIVE_KEYS))
def test_execution_parameters_reject_every_reserved_creative_key(key: str) -> None:
    """B-11 D1: a reserved creative field is never an execution parameter (a second channel outside the identity)."""
    with pytest.raises(DomainValidationError) as err:
        ExecutionSpec(parameters={key: 1})
    assert _code(err) == "spec.execution_reserved"
    # The same name is rejected on the creative side too: a reserved field is first-class only.
    with pytest.raises(DomainValidationError) as err2:
        creative(parameters={key: 1})
    assert _code(err2) == "spec.reserved_key"


def test_reserved_key_set_covers_the_identity_payload_names() -> None:
    payload = creative().identity_payload()
    assert set(payload) - {"parameters"} <= RESERVED_CREATIVE_KEYS
    assert {"capability", "duration_ms", "duration_s", "fps", "seed"} <= RESERVED_CREATIVE_KEYS


@pytest.mark.parametrize("key", sorted(RESERVED_CREATIVE_KEYS))
def test_partition_reports_a_reserved_key_among_execution_parameters(key: str) -> None:
    """A spec built around the constructor (frozen object bypassed) is still caught by the partition check."""
    execution = ExecutionSpec(parameters={"offload": True})
    object.__setattr__(execution, "parameters", {"offload": True, key: 1})
    issues = check_parameter_partition(creative(), execution, {"offload", "width"})
    assert [i.code for i in issues] == ["spec.execution_reserved"]
    assert issues[0].path == f"task.execution.{key}"


def test_partition_accepts_declared_reserved_fields_in_the_binding_list() -> None:
    """Declaring ``width`` execution-only moves the field through ``execution_only_reserved``: it is neither
    an execution parameter nor an undeclared key."""
    execution = ExecutionSpec(parameters={"tile": 1})
    assert check_parameter_partition(creative(), execution, {"tile", "width"}) == ()
    task = TaskSpec(creative=creative(width=512), execution=execution)
    assert task.execution_payload(execution_only_reserved={"width"})["reserved"] == {"width": 512}
    assert "width" not in task.execution_payload(execution_only_reserved={"width"})["parameters"]


def test_authored_form_is_tagged_and_keeps_database_ids() -> None:
    form = creative(seed=None, references=(ref(sha=None),)).authored_form()
    assert form["form"] == "authored"
    assert form["seed"] is None
    assert form["references"][0]["asset_id"] == "asset_reference_1"


# ---------------------------------------------------------------- Take


def take(**kw: Any) -> Take:
    base: dict[str, Any] = {
        "take_id": "take_1",
        "shot_id": "shot_1",
        "number": 1,
        "identity_hash": IDENT,
        "task_fingerprint": TASKFP,
        "seed": 42,
    }
    base.update(kw)
    return Take(**base)


def test_take_requires_its_identity_fields() -> None:
    assert take().seed == 42
    for missing in ("identity_hash", "task_fingerprint", "seed"):
        kwargs: dict[str, Any] = {
            "take_id": "take_1",
            "shot_id": "shot_1",
            "number": 1,
            "identity_hash": IDENT,
            "task_fingerprint": TASKFP,
            "seed": 42,
        }
        del kwargs[missing]
        with pytest.raises(TypeError):
            Take(**kwargs)


def test_take_identity_kinds_and_seed_are_checked() -> None:
    with pytest.raises(DomainValidationError) as err:
        take(identity_hash=TASKFP)
    assert _code(err) == "take.identity_hash"
    with pytest.raises(DomainValidationError) as err2:
        take(task_fingerprint=IDENT)
    assert _code(err2) == "take.task_fingerprint"
    for bad in (None, True, -1, 2**64):
        with pytest.raises(DomainValidationError) as err3:
            take(seed=bad)
        assert _code(err3) == "spec.seed"


def test_take_exact_flag_only_on_reproductions() -> None:
    repro = take(origin=TakeOrigin.REPRODUCTION, source_take_id="take_0", exact=True)
    assert repro.exact is True
    with pytest.raises(DomainValidationError) as err:
        take(origin=TakeOrigin.REPRODUCTION, source_take_id="take_0")
    assert _code(err) == "take.exact"
    with pytest.raises(DomainValidationError) as err2:
        take(exact=False)
    assert _code(err2) == "take.exact"


# ---------------------------------------------------------------- Job and Provenance


def _job(**kw: Any) -> Job:
    base: dict[str, Any] = {
        "job_id": "job_1",
        "project_id": "proj_1",
        "task": TaskSpec(creative=creative()),
        "state": JobState.RUNNING,
        "recovery_policy": RecoveryPolicySnapshot.no_automatic_recovery(),
        "identity_hash": IDENT,
        "task_fingerprint": TASKFP,
    }
    base.update(kw)
    return Job(**base)


def test_running_job_needs_both_fingerprints() -> None:
    assert _job().identity_hash == IDENT
    for missing in ({"identity_hash": None}, {"task_fingerprint": None}):
        with pytest.raises(DomainValidationError) as err:
            _job(**missing)
        assert _code(err) == "job.fingerprints"
    assert (
        _job(state=JobState.QUEUED, identity_hash=None, task_fingerprint=None).state
        is JobState.QUEUED
    )


def test_job_fingerprint_kinds_are_checked() -> None:
    with pytest.raises(DomainValidationError) as err:
        _job(identity_hash=TASKFP)
    assert _code(err) == "job.identity_hash"
    with pytest.raises(DomainValidationError) as err2:
        _job(task_fingerprint=IDENT)
    assert _code(err2) == "job.task_fingerprint"


def test_provenance_fingerprints() -> None:
    generated = Provenance(
        origin=ProvenanceOrigin.GENERATED,
        binding_id="cap.t2i",
        job_id=JobId("job_1"),
        attempt_id=AttemptId("attempt_1"),
        identity_hash=IDENT,
        task_fingerprint=TASKFP,
    )
    assert generated.identity_hash == IDENT
    with pytest.raises(DomainValidationError) as err:
        Provenance(
            origin=ProvenanceOrigin.GENERATED,
            binding_id="cap.t2i",
            job_id=JobId("job_1"),
            attempt_id=AttemptId("attempt_1"),
            identity_hash=TASKFP,
        )
    assert _code(err) == "provenance.identity_hash"
    with pytest.raises(DomainValidationError) as err2:
        Provenance(origin=ProvenanceOrigin.IMPORTED, source_description="disk", identity_hash=IDENT)
    assert _code(err2) == "provenance.imported_producer"


# ---------------------------------------------------------------- snapshot rules over takes


def _codes(*takes: Take, jobs: tuple[Job, ...] = ()) -> list[str]:
    snapshot = DomainSnapshot(
        project=Project(project_id=ProjectId("proj_1"), slug="p", title="T"),
        scenes=(
            Scene(
                scene_id=SceneId("scene_1"), project_id=ProjectId("proj_1"), ordinal=1, title="S"
            ),
        ),
        shots=(
            Shot(
                shot_id=ShotId("shot_1"),
                scene_id=SceneId("scene_1"),
                ordinal=1,
                title="Shot",
                creative=creative(),
            ),
        ),
        takes=takes,
        jobs=jobs,
    )
    return [i.code for i in validate_snapshot(snapshot)]


OTHER_IDENT = fingerprint(FingerprintKind.CREATIVE_IDENTITY, "bb")
OTHER_TASK = fingerprint(FingerprintKind.TASK_FINGERPRINT, "bb")
INCOMPLETE_TASK = Fingerprint(
    kind=FingerprintKind.TASK_FINGERPRINT, digest="aa" * 32, complete=False
)


def test_valid_take_chain_has_no_issues() -> None:
    source = take()
    repro = take(
        take_id="take_2",
        number=2,
        origin=TakeOrigin.REPRODUCTION,
        source_take_id="take_1",
        exact=True,
    )
    assert _codes(source, repro) == []


def test_reproduction_keeps_identity_and_seed() -> None:
    source = take()
    wrong_identity = take(
        take_id="take_2", number=2, origin=TakeOrigin.REPRODUCTION, source_take_id="take_1",
        identity_hash=OTHER_IDENT, exact=False,
    )  # fmt: skip
    wrong_seed = take(
        take_id="take_2", number=2, origin=TakeOrigin.REPRODUCTION, source_take_id="take_1",
        seed=7, exact=True,
    )  # fmt: skip
    assert "recovery.reproduction" in _codes(source, wrong_identity)
    assert "recovery.reproduction" in _codes(source, wrong_seed)


def test_reproduction_exact_flag_matches_the_task_fingerprints() -> None:
    source = take()
    inexact = take(
        take_id="take_2", number=2, origin=TakeOrigin.REPRODUCTION, source_take_id="take_1",
        task_fingerprint=OTHER_TASK, exact=False,
    )  # fmt: skip
    claims_exact = take(
        take_id="take_2", number=2, origin=TakeOrigin.REPRODUCTION, source_take_id="take_1",
        task_fingerprint=OTHER_TASK, exact=True,
    )  # fmt: skip
    assert _codes(source, inexact) == []
    assert _codes(source, claims_exact) == ["recovery.reproduction_exact"]
    incomplete = take(task_fingerprint=INCOMPLETE_TASK)
    same_but_incomplete = take(
        take_id="take_2", number=2, origin=TakeOrigin.REPRODUCTION, source_take_id="take_1",
        task_fingerprint=INCOMPLETE_TASK, exact=True,
    )  # fmt: skip
    assert _codes(incomplete, same_but_incomplete) == ["recovery.reproduction_exact"]


def test_regeneration_must_change_the_identity() -> None:
    source = take()
    same = take(
        take_id="take_2", number=2, origin=TakeOrigin.CREATIVE_REGENERATION, source_take_id="take_1"
    )
    changed = take(
        take_id="take_2", number=2, origin=TakeOrigin.CREATIVE_REGENERATION, source_take_id="take_1",
        identity_hash=OTHER_IDENT,
    )  # fmt: skip
    assert "recovery.regeneration" in _codes(source, same)
    assert _codes(source, changed) == []


def test_adaptation_keeps_the_seed_but_may_keep_or_change_identity() -> None:
    source = take()
    adapted = take(
        take_id="take_2", number=2, origin=TakeOrigin.RESOURCE_ADAPTATION, source_take_id="take_1",
        reason="lower resolution", seed=9,
    )  # fmt: skip
    assert "recovery.adaptation" in _codes(source, adapted)
    ok = take(
        take_id="take_2", number=2, origin=TakeOrigin.RESOURCE_ADAPTATION, source_take_id="take_1",
        reason="lower resolution", task_fingerprint=OTHER_TASK,
    )  # fmt: skip
    assert _codes(source, ok) == []


def test_job_and_take_must_agree_on_fingerprints_and_seed() -> None:
    planned = take(state=TakeState.PLANNED)

    def job(**kw: Any) -> Job:
        args: dict[str, Any] = {"state": JobState.QUEUED, "take_id": "take_1"}
        args.update(kw)
        return _job(**args)

    assert _codes(planned, jobs=(job(),)) == []
    assert _codes(planned, jobs=(job(identity_hash=OTHER_IDENT),)) == ["ref.hash_mismatch"]
    assert _codes(planned, jobs=(job(task_fingerprint=OTHER_TASK),)) == ["ref.hash_mismatch"]
    other_seed = TaskSpec(creative=creative(seed=7))
    assert _codes(planned, jobs=(job(task=other_seed),)) == ["ref.seed_mismatch"]
