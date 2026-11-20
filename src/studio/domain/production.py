"""Production domain: Project -> Scene -> Shot -> Take, plus creative and execution specifications.

Authored intent (Project/Scene/Shot and their ``CreativeSpec``) is what a plan describes; a Take is a runtime
outcome candidate. Nothing here is specific to perfume, ComfyUI, LTX or FFmpeg: a spec names a *capability*
by registry binding id and carries engine-opaque parameters.

Creative vs execution-only (architecture section 3)
* ``CreativeSpec`` is everything that changes *what the output looks/sounds like*. It, and only it, is
  fingerprinted into ``identity_hash`` (``identity_payload``, wrapped by ``studio.domain.fingerprint``).
* ``ExecutionSpec`` is everything that changes *how it is computed* but not the result's identity (offload,
  tiling, threads, ...). It is stored with a task and enters the task fingerprint, never the identity.
The registry binding lists its execution-only keys; ``check_parameter_partition`` enforces that a task puts
each key on the right side. A binding may also declare the reserved fields ``width``, ``height`` and
``duration_s`` execution-only (UD-6, resolution and frame-count adaptation); ``fps`` never.

Typed numeric fields (canonical form v2): ``fps`` is a reduced ``Rational``, the duration is an integer number
of milliseconds, and a seed is an integer written as a decimal string in the identity payload.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Collection
from dataclasses import dataclass, field
from fractions import Fraction
from math import gcd, isfinite
from typing import Any

from studio.domain.assets import AssetRef
from studio.domain.canonical import CANONICAL_VERSION, SAFE_INT_MAX, ParamMap, freeze_params
from studio.domain.errors import UnresolvedSpecError, ValidationIssue, fail
from studio.domain.fingerprint import Fingerprint, FingerprintKind
from studio.domain.ids import (
    ApprovalId,
    ProjectId,
    SceneId,
    ShotId,
    TakeId,
    VersionId,
    check_id,
    check_optional_id,
    check_registry_id,
    check_slug,
    check_text,
    check_unique_ids,
)
from studio.domain.recovery import TakeOrigin
from studio.domain.states import ProjectState, ShotState, TakeState
from studio.domain.vocab import Operation

# First-class creative fields; they may not be repeated as free-form parameter keys.
RESERVED_CREATIVE_KEYS: frozenset[str] = frozenset(
    {
        "operation",
        "binding_id",
        "prompt",
        "negative_prompt",
        "width",
        "height",
        "fps",
        "duration_s",
        "duration_ms",
        "seed",
        "references",
    }
)

# Reserved fields a registry binding may declare execution-only (UD-6, I-1(b)). fps is never adaptable.
RESERVED_EXECUTION_ONLY_ALLOWED: frozenset[str] = frozenset({"width", "height", "duration_s"})

# The identity-payload / execution-payload field name of each allowlisted reserved field.
_RESERVED_PAYLOAD_NAME = {"width": "width", "height": "height", "duration_s": "duration_ms"}

SEED_MAX = 2**64 - 1

_RATIONAL_RE = re.compile(r"^([0-9]{1,15})(?:/([0-9]{1,15}))?$")
_DECIMAL_RE = re.compile(r"^[0-9]{1,15}(?:\.[0-9]{1,15})?$")


def _set(obj: object, name: str, value: Any) -> None:
    object.__setattr__(obj, name, value)


def _positive_int(value: object, path: str, *, maximum: int | None = None) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
        fail("spec.range", path, "must be an integer >= 1")
    if maximum is not None and isinstance(value, int) and value > maximum:
        fail("spec.range", path, f"must be at most {maximum}")


def _required_positive_int(value: object, path: str) -> None:
    if value is None:
        fail("spec.required", path, "required")
    _positive_int(value, path)


@dataclass(frozen=True)
class Rational:
    """A positive rational number in lowest terms (a frame rate such as 24000/1001)."""

    num: int
    den: int = 1

    def __post_init__(self) -> None:
        for name in ("num", "den"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= SAFE_INT_MAX
            ):
                fail("rational.range", f"rational.{name}", "must be an integer in 1 .. 2**53-1")
        divisor = gcd(self.num, self.den)
        _set(self, "num", self.num // divisor)
        _set(self, "den", self.den // divisor)

    @classmethod
    def parse(cls, value: object, path: str = "rational") -> Rational:
        """Accept a ``Rational``, an integer, an integral float (``24.0``) or text ``"24"`` / ``"24000/1001"``.
        A float with a fractional part is rejected: write it as ``num/den``."""
        if isinstance(value, Rational):
            return value
        if isinstance(value, bool):
            fail("rational.type", path, "a boolean is not a rate")
        if isinstance(value, int):
            return cls(value, 1)
        if isinstance(value, float):
            if not isfinite(value) or not value.is_integer():
                fail("rational.float", path, "a fractional rate must be written as 'num/den'")
            return cls(int(value), 1)
        if isinstance(value, str):
            match = _RATIONAL_RE.fullmatch(value)
            if match is None:
                fail("rational.format", path, f"{value!r} is not 'N' or 'N/D'")
            den = match.group(2)
            return cls(int(match.group(1)), 1 if den is None else int(den))
        fail("rational.type", path, f"{type(value).__name__} is not a rate")

    def to_payload(self) -> dict[str, int]:
        return {"num": self.num, "den": self.den}

    def __str__(self) -> str:
        return f"{self.num}/{self.den}"


def duration_ms_from_seconds(value: object, path: str = "duration_s") -> int:
    """Authored seconds (int, float or plain decimal text) as integer milliseconds, rejected unless the value is
    exact to 1 ms. A float is read through its shortest decimal text, so ``0.001`` is 1 ms. Exact arithmetic."""
    if isinstance(value, bool):
        fail("spec.duration_type", path, "a boolean is not a duration")
    if isinstance(value, int):
        seconds = Fraction(value)
    elif isinstance(value, float):
        if not isfinite(value):
            fail("spec.duration_type", path, "NaN and infinity are not durations")
        seconds = Fraction(repr(value))
    elif isinstance(value, str):
        if _DECIMAL_RE.fullmatch(value) is None:
            fail("spec.duration_format", path, f"{value!r} is not a plain decimal number")
        seconds = Fraction(value)
    else:
        fail("spec.duration_type", path, f"{type(value).__name__} is not a duration")
    millis = seconds * 1000
    if millis.denominator != 1:
        fail("spec.duration_precision", path, "a duration must be exact to 1 millisecond")
    if not 1 <= millis.numerator <= SAFE_INT_MAX:
        fail("spec.range", path, "a duration must be at least 1 ms")
    return millis.numerator


def reserved_execution_only_keys(execution_only_keys: Collection[str]) -> frozenset[str]:
    """The reserved creative fields among a binding's ``execution_only`` list (ordinary parameter keys are
    ignored here; they are handled by ``check_parameter_partition``)."""
    return frozenset(execution_only_keys) & RESERVED_CREATIVE_KEYS


def _moved_fields(names: Collection[str]) -> frozenset[str]:
    moved = frozenset(names)
    bad = moved - RESERVED_EXECUTION_ONLY_ALLOWED
    if bad:
        fail(
            "spec.execution_only_reserved",
            "creative",
            f"{sorted(bad)} may not be declared execution-only (allowed: "
            f"{sorted(RESERVED_EXECUTION_ONLY_ALLOWED)}; fps never)",
        )
    return moved


@dataclass(frozen=True, kw_only=True)
class CreativeSpec:
    """What the output should be. Engine-neutral. Result-affecting parameters only."""

    operation: Operation
    binding_id: str | None = (
        None  # requested capability; None = to be chosen when the task is resolved
    )
    prompt: str | None = None
    negative_prompt: str | None = None
    width: int | None = None
    height: int | None = None
    fps: Rational | None = None  # authored 24, 24.0, "24" or "24000/1001" are coerced
    duration_ms: int | None = None
    seed: int | None = (
        None  # None until resolved; a resolved task always has one (architecture section 5)
    )
    references: tuple[AssetRef, ...] = ()
    parameters: ParamMap = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.binding_id is not None:
            check_registry_id(self.binding_id, "creative.binding_id")
        for name in ("prompt", "negative_prompt"):
            value = getattr(self, name)
            if value is not None:
                check_text(value, f"creative.{name}", allow_empty=True)
        _positive_int(self.width, "creative.width", maximum=SAFE_INT_MAX)
        _positive_int(self.height, "creative.height", maximum=SAFE_INT_MAX)
        if self.fps is not None:
            _set(self, "fps", Rational.parse(self.fps, "creative.fps"))
        _positive_int(self.duration_ms, "creative.duration_ms", maximum=SAFE_INT_MAX)
        if self.seed is not None and (
            isinstance(self.seed, bool)
            or not isinstance(self.seed, int)
            or not 0 <= self.seed <= SEED_MAX
        ):
            fail("spec.seed", "creative.seed", "seed must be an integer in 0 .. 2**64-1")
        references = tuple(self.references)
        for i, ref in enumerate(references):
            if not isinstance(ref, AssetRef):
                fail(
                    "spec.reference_type",
                    f"creative.references[{i}]",
                    f"expected an AssetRef, got {type(ref).__name__}",
                )
        slots = Counter((r.role, r.index) for r in references)
        for (role, index), count in sorted(slots.items()):
            if count > 1:
                fail(
                    "spec.reference_duplicate",
                    f"creative.references[{role}#{index}]",
                    "the (role, index) pair appears more than once",
                )
        indices: dict[str, list[int]] = {}
        for ref in references:
            indices.setdefault(ref.role, []).append(ref.index)
        for role, numbers in sorted(indices.items()):
            if sorted(numbers) != list(range(1, len(numbers) + 1)):
                fail(
                    "spec.reference_index",
                    f"creative.references[{role}]",
                    "indices of a role must be exactly 1..n",
                )
        _set(self, "references", references)
        _set(self, "parameters", freeze_params(self.parameters, "creative.parameters"))
        clash = RESERVED_CREATIVE_KEYS & set(self.parameters)
        if clash:
            fail(
                "spec.reserved_key",
                "creative.parameters",
                f"keys {sorted(clash)} are first-class fields, not free parameters",
            )

    @property
    def is_resolved(self) -> bool:
        """Seed fixed, capability chosen, every reference pinned to content."""
        return (
            self.binding_id is not None
            and self.seed is not None
            and all(r.is_resolved for r in self.references)
        )

    def _sorted_references(self) -> list[AssetRef]:
        return sorted(self.references, key=lambda r: (r.role, r.index))

    def identity_payload(self, *, execution_only_reserved: Collection[str] = ()) -> dict[str, Any]:
        """Payload of the creative identity hash (design B.3). Requires a resolved spec
        (``UnresolvedSpecError`` otherwise). ``execution_only_reserved`` are the reserved fields the binding
        declares execution-only (subset of ``width``/``height``/``duration_s``); they leave the identity and
        go to ``execution_reserved_values``. The seed is a decimal string; references are sorted by
        ``(role, index)`` so authoring order never changes the identity."""
        if not self.is_resolved or self.seed is None:
            raise UnresolvedSpecError(
                "creative spec needs binding_id, seed and content-pinned references"
            )
        moved = _moved_fields(execution_only_reserved)
        payload: dict[str, Any] = {
            "operation": self.operation,
            "capability": self.binding_id,
            "prompt": self.prompt,
            "negative_prompt": self.negative_prompt,
            "width": self.width,
            "height": self.height,
            "fps": None if self.fps is None else self.fps.to_payload(),
            "duration_ms": self.duration_ms,
            "seed": str(self.seed),
            "references": [r.identity_form() for r in self._sorted_references()],
            "parameters": self.parameters,
        }
        for name in moved:
            del payload[_RESERVED_PAYLOAD_NAME[name]]
        return payload

    def execution_reserved_values(
        self, execution_only_reserved: Collection[str] = ()
    ) -> dict[str, Any]:
        """The values of the reserved fields that were moved out of the identity (they belong to execution)."""
        values = {
            "width": self.width,
            "height": self.height,
            "duration_ms": self.duration_ms,
        }
        moved = _moved_fields(execution_only_reserved)
        return {_RESERVED_PAYLOAD_NAME[n]: values[_RESERVED_PAYLOAD_NAME[n]] for n in sorted(moved)}

    def authored_form(self) -> dict[str, Any]:
        """Plain structure describing authored intent (for plan diffs). Not fingerprinted and never equal to an
        identity payload: it tags itself and keeps database ids."""
        return {
            "form": "authored",
            "canonical_version": CANONICAL_VERSION,
            "operation": self.operation,
            "binding_id": self.binding_id,
            "prompt": self.prompt,
            "negative_prompt": self.negative_prompt,
            "width": self.width,
            "height": self.height,
            "fps": None if self.fps is None else self.fps.to_payload(),
            "duration_ms": self.duration_ms,
            "seed": None if self.seed is None else str(self.seed),
            "references": [r.authored_form() for r in self._sorted_references()],
            "parameters": self.parameters,
        }


@dataclass(frozen=True, kw_only=True)
class ExecutionSpec:
    """How to compute it. Never part of the identity; changing it does not make a different creative result."""

    parameters: ParamMap = field(default_factory=dict)

    def __post_init__(self) -> None:
        _set(self, "parameters", freeze_params(self.parameters, "execution.parameters"))

    def canonical_form(self) -> dict[str, Any]:
        """For storage and diagnostics only; the task fingerprint uses ``TaskSpec.execution_payload``."""
        return {
            "kind": "execution_spec",
            "canonical_version": CANONICAL_VERSION,
            "parameters": self.parameters,
        }


def check_parameter_partition(
    creative: CreativeSpec,
    execution: ExecutionSpec,
    execution_only_keys: Collection[str],
    path: str = "task",
) -> tuple[ValidationIssue, ...]:
    """Each parameter key must live on exactly the right side.

    ``execution_only_keys`` is the ``execution_only`` list of the registry binding, passed in by the caller
    (the domain does not read the registry). Rules: no key on both sides; no execution-only key among the
    creative parameters; no undeclared key among the execution parameters (an undeclared key would silently
    escape the fingerprint if it is in fact creative).
    """
    declared = set(execution_only_keys)
    issues: list[ValidationIssue] = []
    creative_keys = set(creative.parameters)
    execution_keys = set(execution.parameters)
    for key in sorted(creative_keys & execution_keys):
        issues.append(
            ValidationIssue(
                "params.both_sides", f"{path}.{key}", "key is both creative and execution"
            )
        )
    for key in sorted(creative_keys & declared):
        issues.append(
            ValidationIssue(
                "params.execution_only_in_creative",
                f"{path}.creative.{key}",
                "declared execution-only by the binding",
            )
        )
    for key in sorted(execution_keys - declared):
        issues.append(
            ValidationIssue(
                "params.undeclared_execution",
                f"{path}.execution.{key}",
                "not declared execution-only by the binding",
            )
        )
    return tuple(issues)


@dataclass(frozen=True, kw_only=True)
class Project:
    project_id: ProjectId
    slug: str
    title: str
    state: ProjectState = ProjectState.DRAFT
    description: str = ""

    def __post_init__(self) -> None:
        check_id(self.project_id, "project.project_id")
        check_slug(self.slug, "project.slug")
        check_text(self.title, "project.title")
        check_text(self.description, "project.description", allow_empty=True)


@dataclass(frozen=True, kw_only=True)
class Scene:
    scene_id: SceneId
    project_id: ProjectId
    ordinal: int
    title: str
    summary: str = ""

    def __post_init__(self) -> None:
        check_id(self.scene_id, "scene.scene_id")
        check_id(self.project_id, "scene.project_id")
        _required_positive_int(self.ordinal, "scene.ordinal")
        check_text(self.title, "scene.title")
        check_text(self.summary, "scene.summary", allow_empty=True)


@dataclass(frozen=True, kw_only=True)
class Shot:
    shot_id: ShotId
    scene_id: SceneId
    ordinal: int
    title: str
    creative: CreativeSpec
    execution: ExecutionSpec = field(default_factory=ExecutionSpec)
    state: ShotState = ShotState.DRAFT
    selected_take_id: TakeId | None = None
    notes: str = ""

    def __post_init__(self) -> None:
        check_id(self.shot_id, "shot.shot_id")
        check_id(self.scene_id, "shot.scene_id")
        _required_positive_int(self.ordinal, "shot.ordinal")
        check_text(self.title, "shot.title")
        check_optional_id(self.selected_take_id, "shot.selected_take_id")
        check_text(self.notes, "shot.notes", allow_empty=True)


_NO_OUTPUT_STATES = frozenset(
    {TakeState.PLANNED, TakeState.GENERATING, TakeState.FAILED, TakeState.CANCELED}
)


@dataclass(frozen=True, kw_only=True)
class Take:
    """One generation outcome candidate for a shot.

    A take is created from a resolved task, so it always owns its identity: ``identity_hash`` (creative),
    ``task_fingerprint`` (reproduction and cache) and ``seed``, persisted before its first attempt.
    Infrastructure retries and resume happen *inside* a take (new attempts); creative regeneration, resource
    adaptation and reproduction each create a *new* take that points at its ``source_take_id`` (see
    ``studio.domain.recovery``). A reproduction records whether it is ``exact`` (same task fingerprint, both
    complete) or only the same creative identity and seed.
    """

    take_id: TakeId
    shot_id: ShotId
    number: int  # 1-based, unique within the shot
    identity_hash: Fingerprint
    task_fingerprint: Fingerprint
    seed: int
    state: TakeState = TakeState.PLANNED
    origin: TakeOrigin = TakeOrigin.INITIAL
    source_take_id: TakeId | None = None
    reason: str = ""  # why this take exists (required for resource adaptation)
    exact: bool | None = None  # REPRODUCTION only
    output_version_ids: tuple[VersionId, ...] = ()
    adaptation_approval_id: ApprovalId | None = None

    def __post_init__(self) -> None:
        check_id(self.take_id, "take.take_id")
        check_id(self.shot_id, "take.shot_id")
        _required_positive_int(self.number, "take.number")
        check_optional_id(self.source_take_id, "take.source_take_id")
        check_text(self.reason, "take.reason", allow_empty=True)
        if (
            not isinstance(self.identity_hash, Fingerprint)
            or self.identity_hash.kind is not FingerprintKind.CREATIVE_IDENTITY
        ):
            fail("take.identity_hash", "take.identity_hash", "a creative identity fingerprint")
        if (
            not isinstance(self.task_fingerprint, Fingerprint)
            or self.task_fingerprint.kind is not FingerprintKind.TASK_FINGERPRINT
        ):
            fail("take.task_fingerprint", "take.task_fingerprint", "a task fingerprint")
        if (
            isinstance(self.seed, bool)
            or not isinstance(self.seed, int)
            or not 0 <= self.seed <= SEED_MAX
        ):
            fail("spec.seed", "take.seed", "seed must be an integer in 0 .. 2**64-1")
        _set(
            self,
            "output_version_ids",
            check_unique_ids(self.output_version_ids, "take.output_version_ids"),
        )
        check_optional_id(self.adaptation_approval_id, "take.adaptation_approval_id")

        if self.origin is TakeOrigin.REPRODUCTION:
            if not isinstance(self.exact, bool):
                fail("take.exact", "take.exact", "a reproduction records whether it is exact")
        elif self.exact is not None:
            fail("take.exact", "take.exact", "only a reproduction records 'exact'")

        if self.origin is TakeOrigin.INITIAL:
            if self.source_take_id is not None:
                fail("take.source", "take.source_take_id", "an initial take has no source take")
        else:
            if self.source_take_id is None:
                fail("take.source", "take.source_take_id", f"{self.origin} needs a source take")
            if self.source_take_id == self.take_id:
                fail("take.source", "take.source_take_id", "a take cannot be its own source")
        if self.origin is TakeOrigin.RESOURCE_ADAPTATION:
            if not self.reason.strip():
                fail(
                    "take.reason",
                    "take.reason",
                    "resource adaptation must state what was adapted and why",
                )
            if (
                self.state not in (TakeState.PLANNED, TakeState.CANCELED)
                and self.adaptation_approval_id is None
            ):
                fail(
                    "take.adaptation_unapproved",
                    "take.adaptation_approval_id",
                    "an adapted take may not progress past 'planned' without an approval reference",
                )
        elif self.adaptation_approval_id is not None:
            fail(
                "take.adaptation_approval",
                "take.adaptation_approval_id",
                "only adapted takes carry one",
            )

        if self.state in _NO_OUTPUT_STATES and self.output_version_ids:
            fail("take.outputs", "take.output_version_ids", f"a {self.state} take has no outputs")
        if self.state in (TakeState.PRODUCED, TakeState.APPROVED) and not self.output_version_ids:
            fail("take.outputs", "take.output_version_ids", f"a {self.state} take needs outputs")
