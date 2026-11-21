"""Fingerprint envelopes (UD-2): SHA-256 over canonical form v2, with the kind and version inside the hash.

Three digest families (docs/design/m0.3a-domain-amendments.md section B):

1. *Object digest*: SHA-256 of raw bytes (``ObjectRef.sha256``). Not enveloped: the bytes are the identity.
2. *Creative identity hash* (``CREATIVE_IDENTITY``): drives approval staleness (UD-3) and the UD-6 test
   "was only an execution setting changed?".
3. *Task fingerprint* (``TASK_FINGERPRINT``): drives reproduction, caching and resume compatibility.

``RECOVERY_POLICY``, ``EXECUTION_SPEC`` (diagnostics) and ``STAGE_KEY`` share the same envelope. The kind and
the canonical version are part of the hashed bytes, so equal payloads of different kinds or versions never
collide, and a ``Fingerprint`` of one version is never "equal" to one of another (they are *incomparable*).

``complete=False`` marks a fingerprint with an unknown component (for example a model file whose digest is not
recorded). Such a fingerprint is stored and displayed but must never serve for caching or deduplication.

Pure functions over plain data. ``hashlib`` is standard library.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from studio.domain.canonical import CANONICAL_VERSION, SAFE_INT_MAX, canonical_bytes
from studio.domain.errors import fail
from studio.domain.ids import (
    ROLE_RE,
    check_id,
    check_registry_id,
    check_sha256,
    check_text,
)

FINGERPRINT_ALG = "sha256"


class FingerprintKind(StrEnum):
    CREATIVE_IDENTITY = "creative_identity"
    TASK_FINGERPRINT = "task_fingerprint"
    EXECUTION_SPEC = "execution_spec"
    RECOVERY_POLICY = "recovery_policy"
    STAGE_KEY = "stage_key"


def envelope(
    kind: FingerprintKind, payload: Any, *, canonical_version: int = CANONICAL_VERSION
) -> dict[str, Any]:
    """The structure whose canonical bytes are hashed."""
    return {
        "alg": FINGERPRINT_ALG,
        "canonical_version": canonical_version,
        "kind": kind,
        "payload": payload,
    }


@dataclass(frozen=True, kw_only=True)
class Fingerprint:
    """A typed digest. Equality compares every field, so kinds and versions never mix."""

    kind: FingerprintKind
    digest: str
    complete: bool = True
    alg: str = FINGERPRINT_ALG
    canonical_version: int = CANONICAL_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.kind, FingerprintKind):
            fail("fingerprint.kind", "fingerprint.kind", "kind must be a FingerprintKind")
        check_sha256(self.digest, "fingerprint.digest")
        if self.alg != FINGERPRINT_ALG:
            fail("fingerprint.alg", "fingerprint.alg", f"only {FINGERPRINT_ALG} is supported")
        if (
            isinstance(self.canonical_version, bool)
            or not isinstance(self.canonical_version, int)
            or self.canonical_version < 1
        ):
            fail(
                "fingerprint.version",
                "fingerprint.canonical_version",
                "must be an integer >= 1",
            )
        if not isinstance(self.complete, bool):
            fail("fingerprint.complete", "fingerprint.complete", "must be a bool")

    @property
    def key(self) -> str:
        """Text form for logs and the database: ``<kind>/v<version>/sha256:<digest>``."""
        return f"{self.kind.value}/v{self.canonical_version}/{self.alg}:{self.digest}"

    def comparable_with(self, other: Fingerprint) -> bool:
        """Same kind and same canonical version: only then can digest equality mean anything."""
        return self.kind is other.kind and self.canonical_version == other.canonical_version


def compute_fingerprint(
    kind: FingerprintKind, payload: Any, *, complete: bool = True
) -> Fingerprint:
    digest = hashlib.sha256(canonical_bytes(envelope(kind, payload))).hexdigest()
    return Fingerprint(kind=kind, digest=digest, complete=complete)


def same_for_caching(a: Fingerprint, b: Fingerprint) -> bool:
    """True only for two equal *complete* fingerprints. Two incomplete ones are never a cache hit."""
    return a.complete and b.complete and a == b


# ---------------------------------------------------------------- task fingerprint components


def _non_negative_size(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= SAFE_INT_MAX:
        fail("component.size", path, "size must be an integer in 0 .. 2**53-1")
    return value


@dataclass(frozen=True, kw_only=True)
class ModelComponent:
    """One model a task uses. A ``None`` digest or size means *not recorded* (the fingerprint turns incomplete).
    A directory model uses the ``manifest_digest`` of its files as ``sha256``."""

    model_id: str
    role: str
    sha256: str | None = None
    size_bytes: int | None = None

    def __post_init__(self) -> None:
        check_registry_id(self.model_id, "model.model_id")
        check_id(self.role, "model.role", ROLE_RE)
        if self.sha256 is not None:
            check_sha256(self.sha256, "model.sha256")
        if self.size_bytes is not None:
            _non_negative_size(self.size_bytes, "model.size_bytes")

    @property
    def is_known(self) -> bool:
        return self.sha256 is not None and self.size_bytes is not None

    def payload(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "role": self.role,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True, kw_only=True)
class EngineComponent:
    """The engine that computes the result, with its version (for example a git commit or a version string).
    ``version=None`` means unknown."""

    engine_id: str
    version: str | None = None

    def __post_init__(self) -> None:
        check_registry_id(self.engine_id, "engine.engine_id")
        if self.version is not None:
            check_text(self.version, "engine.version")

    @property
    def is_known(self) -> bool:
        return self.version is not None

    def payload(self) -> dict[str, Any]:
        return {"engine_id": self.engine_id, "version": self.version}


@dataclass(frozen=True, kw_only=True)
class ManifestEntry:
    """One file of a directory model: a relative POSIX path, its size and its object digest."""

    path: str
    size_bytes: int
    sha256: str

    def __post_init__(self) -> None:
        check_text(self.path, "manifest.path")
        segments = self.path.split("/")
        if (
            "\\" in self.path
            or self.path.startswith("/")
            or any(s in ("", ".", "..") for s in segments)
        ):
            fail("manifest.path", "manifest.path", "must be a relative POSIX path without '..'")
        _non_negative_size(self.size_bytes, "manifest.size_bytes")
        check_sha256(self.sha256, "manifest.sha256")


def manifest_digest(entries: Iterable[ManifestEntry]) -> str:
    """Digest of a directory model: SHA-256 of the canonical bytes of its manifest sorted by path."""
    ordered = sorted(entries, key=lambda e: e.path)
    if not ordered:
        fail("manifest.empty", "manifest", "a manifest lists at least one file")
    paths = [e.path for e in ordered]
    if len(set(paths)) != len(paths):
        fail("manifest.duplicate", "manifest", "duplicate path")
    listing = [{"path": e.path, "size_bytes": e.size_bytes, "sha256": e.sha256} for e in ordered]
    return hashlib.sha256(canonical_bytes(listing)).hexdigest()


def _sorted_models(models: Iterable[ModelComponent]) -> list[ModelComponent]:
    ordered = sorted(models, key=lambda m: (m.model_id, m.role))
    keys = [(m.model_id, m.role) for m in ordered]
    if len(set(keys)) != len(keys):
        fail("task.model_duplicate", "task.models", "a (model_id, role) pair appears twice")
    return ordered


def _check_identity(identity: Fingerprint) -> None:
    if identity.kind is not FingerprintKind.CREATIVE_IDENTITY:
        fail("task.identity_kind", "task.identity", "expected a creative identity fingerprint")
    if identity.canonical_version != CANONICAL_VERSION:
        fail("task.identity_version", "task.identity", "identity of another canonical version")


def task_payload(
    *,
    identity: Fingerprint,
    execution: Mapping[str, Any],
    models: Iterable[ModelComponent],
    template_sha256: str | None,
    engine: EngineComponent,
) -> dict[str, Any]:
    """Payload of the task fingerprint (design B.3). Component digests keep "what changed" cheap to report.
    Input object hashes and the seed are covered through ``identity``."""
    _check_identity(identity)
    if template_sha256 is not None:
        check_sha256(template_sha256, "task.template")
    return {
        "identity": identity.digest,
        "execution": execution,
        "models": [m.payload() for m in _sorted_models(models)],
        "template": template_sha256,
        "engine": engine.payload(),
    }


def compute_task_fingerprint(
    *,
    identity: Fingerprint,
    execution: Mapping[str, Any],
    models: Iterable[ModelComponent],
    template_sha256: str | None,
    engine: EngineComponent,
) -> Fingerprint:
    """The task fingerprint. ``complete`` only if the identity is complete, every model digest and size is
    recorded and the engine version is known. ``template_sha256=None`` means "no template" and counts as known."""
    model_list = list(models)
    payload = task_payload(
        identity=identity,
        execution=execution,
        models=model_list,
        template_sha256=template_sha256,
        engine=engine,
    )
    complete = identity.complete and all(m.is_known for m in model_list) and engine.is_known
    return compute_fingerprint(FingerprintKind.TASK_FINGERPRINT, payload, complete=complete)


def execution_fingerprint(execution: Mapping[str, Any]) -> Fingerprint:
    """Diagnostics digest of the execution parameters. Not an input of the creative identity."""
    return compute_fingerprint(FingerprintKind.EXECUTION_SPEC, execution)


def stage_key(task: Fingerprint, stage: str) -> Fingerprint:
    """Resume compatibility key: an intermediate artifact may be reused only for the same task fingerprint and
    stage. Complete only if the task fingerprint is."""
    if task.kind is not FingerprintKind.TASK_FINGERPRINT:
        fail("stage_key.kind", "stage_key.task", "expected a task fingerprint")
    check_id(stage, "stage_key.stage", ROLE_RE)
    return compute_fingerprint(
        FingerprintKind.STAGE_KEY,
        {"stage": stage, "task_fingerprint": task.digest},
        complete=task.complete,
    )
