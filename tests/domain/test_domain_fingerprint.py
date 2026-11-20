"""Fingerprint envelopes: kinds, versions, completeness, task payload and stage keys."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from studio.domain.canonical import CANONICAL_VERSION, canonical_bytes
from studio.domain.errors import DomainValidationError
from studio.domain.fingerprint import (
    EngineComponent,
    Fingerprint,
    FingerprintKind,
    ManifestEntry,
    ModelComponent,
    compute_fingerprint,
    compute_task_fingerprint,
    envelope,
    execution_fingerprint,
    manifest_digest,
    same_for_caching,
    stage_key,
    task_payload,
)

SHA_A = "ab" * 32
SHA_B = "cd" * 32
SHA_C = "ef" * 32


def _code(excinfo: pytest.ExceptionInfo[DomainValidationError]) -> str:
    return excinfo.value.issues[0].code


def _identity(digest: str = SHA_A, *, complete: bool = True) -> Fingerprint:
    return Fingerprint(kind=FingerprintKind.CREATIVE_IDENTITY, digest=digest, complete=complete)


def _model(model_id: str = "m.a", role: str = "unet", **kw: Any) -> ModelComponent:
    kw.setdefault("sha256", SHA_B)
    kw.setdefault("size_bytes", 10)
    return ModelComponent(model_id=model_id, role=role, **kw)


def _engine(version: str | None = "1.0") -> EngineComponent:
    return EngineComponent(engine_id="e.a", version=version)


def _task(**override: Any) -> Fingerprint:
    args: dict[str, Any] = {
        "identity": _identity(),
        "execution": {"parameters": {}, "reserved": {}},
        "models": [_model()],
        "template_sha256": None,
        "engine": _engine(),
    }
    args.update(override)
    return compute_task_fingerprint(**args)


def test_envelope_shape_and_digest() -> None:
    env = envelope(FingerprintKind.CREATIVE_IDENTITY, {"a": 1})
    assert env == {
        "alg": "sha256",
        "canonical_version": CANONICAL_VERSION,
        "kind": FingerprintKind.CREATIVE_IDENTITY,
        "payload": {"a": 1},
    }
    fp = compute_fingerprint(FingerprintKind.CREATIVE_IDENTITY, {"a": 1})
    assert fp.digest == hashlib.sha256(canonical_bytes(env)).hexdigest()
    assert fp.complete is True


def test_key_format() -> None:
    fp = compute_fingerprint(FingerprintKind.TASK_FINGERPRINT, {"a": 1})
    assert fp.key == f"task_fingerprint/v{CANONICAL_VERSION}/sha256:{fp.digest}"


def test_same_payload_different_kind_never_equal() -> None:
    kinds = list(FingerprintKind)
    prints = [compute_fingerprint(k, {"a": 1}) for k in kinds]
    assert len({p.digest for p in prints}) == len(kinds)
    for i, a in enumerate(prints):
        for b in prints[i + 1 :]:
            assert a != b
            assert not a.comparable_with(b)


def test_different_canonical_version_is_incomparable() -> None:
    a = Fingerprint(kind=FingerprintKind.CREATIVE_IDENTITY, digest=SHA_A, canonical_version=1)
    b = Fingerprint(kind=FingerprintKind.CREATIVE_IDENTITY, digest=SHA_A, canonical_version=3)
    assert a != b
    assert not a.comparable_with(b)
    assert a.comparable_with(a)


def test_fingerprint_validation() -> None:
    with pytest.raises(DomainValidationError) as err:
        Fingerprint(kind=FingerprintKind.STAGE_KEY, digest="xyz")
    assert _code(err) == "sha256.invalid"
    with pytest.raises(DomainValidationError) as err2:
        Fingerprint(kind=FingerprintKind.STAGE_KEY, digest=SHA_A, alg="md5")
    assert _code(err2) == "fingerprint.alg"
    with pytest.raises(DomainValidationError) as err3:
        Fingerprint(kind=FingerprintKind.STAGE_KEY, digest=SHA_A, canonical_version=0)
    assert _code(err3) == "fingerprint.version"


def test_incomplete_fingerprints_are_never_a_cache_hit() -> None:
    incomplete = _identity(complete=False)
    assert incomplete == _identity(complete=False)
    assert not same_for_caching(incomplete, incomplete)
    assert same_for_caching(_identity(), _identity())
    assert not same_for_caching(_identity(), _identity(SHA_B))
    assert not same_for_caching(_identity(), incomplete)


def test_task_fingerprint_is_sensitive_to_every_component() -> None:
    base = _task()
    assert _task() == base
    assert _task(identity=_identity(SHA_B)) != base
    assert _task(execution={"parameters": {"tile": 1}, "reserved": {}}) != base
    assert _task(models=[_model(sha256=SHA_C)]) != base
    assert _task(template_sha256=SHA_C) != base
    assert _task(engine=_engine("2.0")) != base


def test_models_are_order_independent_and_unique() -> None:
    m1, m2 = _model("m.a", "unet"), _model("m.b", "vae")
    assert _task(models=[m1, m2]) == _task(models=[m2, m1])
    with pytest.raises(DomainValidationError) as err:
        _task(models=[m1, _model("m.a", "unet", sha256=SHA_C)])
    assert _code(err) == "task.model_duplicate"


def test_completeness_rules() -> None:
    assert _task().complete is True
    assert _task(template_sha256=None).complete is True
    assert _task(models=[_model(sha256=None)]).complete is False
    assert _task(models=[_model(size_bytes=None)]).complete is False
    assert _task(engine=_engine(None)).complete is False
    assert _task(identity=_identity(complete=False)).complete is False


def test_identity_must_be_a_current_creative_identity() -> None:
    wrong_kind = Fingerprint(kind=FingerprintKind.TASK_FINGERPRINT, digest=SHA_A)
    with pytest.raises(DomainValidationError) as err:
        _task(identity=wrong_kind)
    assert _code(err) == "task.identity_kind"
    old = Fingerprint(
        kind=FingerprintKind.CREATIVE_IDENTITY,
        digest=SHA_A,
        canonical_version=CANONICAL_VERSION + 1,
    )
    with pytest.raises(DomainValidationError) as err2:
        _task(identity=old)
    assert _code(err2) == "task.identity_version"


def test_task_payload_lists_components() -> None:
    payload = task_payload(
        identity=_identity(),
        execution={"parameters": {}, "reserved": {}},
        models=[_model()],
        template_sha256=SHA_C,
        engine=_engine(),
    )
    assert payload["identity"] == SHA_A
    assert payload["template"] == SHA_C
    assert payload["engine"] == {"engine_id": "e.a", "version": "1.0"}
    assert payload["models"] == [
        {"model_id": "m.a", "role": "unet", "sha256": SHA_B, "size_bytes": 10}
    ]


def test_component_validation() -> None:
    with pytest.raises(DomainValidationError):
        ModelComponent(model_id="Bad Id", role="unet")
    with pytest.raises(DomainValidationError) as err:
        ModelComponent(model_id="m.a", role="unet", size_bytes=-1)
    assert _code(err) == "component.size"
    with pytest.raises(DomainValidationError):
        ModelComponent(model_id="m.a", role="unet", sha256="short")
    with pytest.raises(DomainValidationError):
        EngineComponent(engine_id="e.a", version="  ")


def test_execution_fingerprint_is_its_own_kind() -> None:
    fp = execution_fingerprint({"parameters": {"tile": 1}, "reserved": {}})
    assert fp.kind is FingerprintKind.EXECUTION_SPEC


def test_stage_key_depends_on_task_and_stage() -> None:
    task = _task()
    key = stage_key(task, "stage_one")
    assert key == stage_key(task, "stage_one")
    assert key != stage_key(task, "stage_two")
    assert key != stage_key(_task(identity=_identity(SHA_B)), "stage_one")
    assert key.kind is FingerprintKind.STAGE_KEY
    assert key.complete is True
    assert stage_key(_task(engine=_engine(None)), "stage_one").complete is False


def test_stage_key_requires_a_task_fingerprint_and_a_valid_stage() -> None:
    with pytest.raises(DomainValidationError) as err:
        stage_key(_identity(), "stage_one")
    assert _code(err) == "stage_key.kind"
    with pytest.raises(DomainValidationError):
        stage_key(_task(), "Not A Stage")


def test_manifest_digest() -> None:
    a = ManifestEntry(path="unet/model.bin", size_bytes=5, sha256=SHA_A)
    b = ManifestEntry(path="vae/model.bin", size_bytes=7, sha256=SHA_B)
    assert manifest_digest([a, b]) == manifest_digest([b, a])
    changed = ManifestEntry(path="vae/model.bin", size_bytes=7, sha256=SHA_C)
    assert manifest_digest([a, changed]) != manifest_digest([a, b])
    with pytest.raises(DomainValidationError) as err:
        manifest_digest([])
    assert _code(err) == "manifest.empty"
    with pytest.raises(DomainValidationError) as err2:
        manifest_digest([a, a])
    assert _code(err2) == "manifest.duplicate"


@pytest.mark.parametrize("path", ["/abs/file", "a\\b", "a/../b", "a//b", "./a", ""])
def test_manifest_path_rules(path: str) -> None:
    with pytest.raises(DomainValidationError):
        ManifestEntry(path=path, size_bytes=1, sha256=SHA_A)
