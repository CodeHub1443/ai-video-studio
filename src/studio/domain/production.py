"""Production domain: Project -> Scene -> Shot -> Take, plus creative and execution specifications.

Authored intent (Project/Scene/Shot and their ``CreativeSpec``) is what a plan describes; a Take is a runtime
outcome candidate. Nothing here is specific to perfume, ComfyUI, LTX or FFmpeg: a spec names a *capability*
by registry binding id and carries engine-opaque parameters.

Creative vs execution-only (architecture section 3)
* ``CreativeSpec`` is everything that changes *what the output looks/sounds like*. It, and only it, is
  fingerprinted into ``spec_hash`` (by a later milestone, from ``canonical_form(resolved=True)``).
* ``ExecutionSpec`` is everything that changes *how it is computed* but not the result's identity (offload,
  tiling, threads, ...). It is stored with a task but never fingerprinted.
The registry binding lists its execution-only keys; ``check_parameter_partition`` enforces that a task puts
each key on the right side.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Any

from studio.domain.assets import AssetRef
from studio.domain.canonical import CANONICAL_VERSION, ParamMap, freeze_params
from studio.domain.errors import UnresolvedSpecError, ValidationIssue, fail
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
    check_sha256,
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
        "seed",
        "references",
    }
)

SEED_MAX = 2**64 - 1


def _set(obj: object, name: str, value: Any) -> None:
    object.__setattr__(obj, name, value)


def _positive_int(value: object, path: str) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
        fail("spec.range", path, "must be an integer >= 1")


def _required_positive_int(value: object, path: str) -> None:
    if value is None:
        fail("spec.required", path, "required")
    _positive_int(value, path)


def _positive_number(value: object, path: str) -> None:
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0
    ):
        fail("spec.range", path, "must be a number > 0")


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
    fps: float | None = None
    duration_s: float | None = None
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
        _positive_int(self.width, "creative.width")
        _positive_int(self.height, "creative.height")
        _positive_number(self.fps, "creative.fps")
        _positive_number(self.duration_s, "creative.duration_s")
        if self.seed is not None and (
            isinstance(self.seed, bool)
            or not isinstance(self.seed, int)
            or not 0 <= self.seed <= SEED_MAX
        ):
            fail("spec.seed", "creative.seed", "seed must be an integer in 0 .. 2**64-1")
        _set(self, "references", tuple(self.references))
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

    def canonical_form(self, *, resolved: bool) -> dict[str, Any]:
        """Hash-ready plain structure (feed it to ``studio.domain.canonical.canonical_bytes``).

        ``resolved=True`` (the future ``spec_hash`` input) requires a seed, a binding and content-pinned
        references and raises ``UnresolvedSpecError`` otherwise. ``resolved=False`` describes authored intent
        (e.g. for plan diffs) and tags itself so the two forms can never collide.
        """
        if resolved and not self.is_resolved:
            raise UnresolvedSpecError(
                "creative spec needs binding_id, seed and content-pinned references"
            )
        return {
            "kind": "creative_spec",
            "canonical_version": CANONICAL_VERSION,
            "form": "resolved" if resolved else "authored",
            "operation": self.operation,
            "binding_id": self.binding_id,
            "prompt": self.prompt,
            "negative_prompt": self.negative_prompt,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "duration_s": self.duration_s,
            "seed": self.seed,
            "references": [r.canonical_form(resolved=resolved) for r in self.references],
            "parameters": self.parameters,
        }


@dataclass(frozen=True, kw_only=True)
class ExecutionSpec:
    """How to compute it. Never fingerprinted; changing it does not make a different take."""

    parameters: ParamMap = field(default_factory=dict)

    def __post_init__(self) -> None:
        _set(self, "parameters", freeze_params(self.parameters, "execution.parameters"))

    def canonical_form(self) -> dict[str, Any]:
        """For storage and diagnostics only; deliberately not an input of any fingerprint."""
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

    A take owns an identity (``spec_hash`` + ``seed``, persisted before its first attempt) and the outputs
    of its job. Infrastructure retries and resume happen *inside* a take (new attempts); creative
    regeneration, resource adaptation and reproduction each create a *new* take that points at its
    ``source_take_id`` (see ``studio.domain.recovery``).
    """

    take_id: TakeId
    shot_id: ShotId
    number: int  # 1-based, unique within the shot
    state: TakeState = TakeState.PLANNED
    origin: TakeOrigin = TakeOrigin.INITIAL
    source_take_id: TakeId | None = None
    reason: str = ""  # why this take exists (required for resource adaptation)
    spec_hash: str | None = None
    seed: int | None = None
    output_version_ids: tuple[VersionId, ...] = ()
    adaptation_approval_id: ApprovalId | None = None

    def __post_init__(self) -> None:
        check_id(self.take_id, "take.take_id")
        check_id(self.shot_id, "take.shot_id")
        _required_positive_int(self.number, "take.number")
        check_optional_id(self.source_take_id, "take.source_take_id")
        check_text(self.reason, "take.reason", allow_empty=True)
        if self.spec_hash is not None:
            check_sha256(self.spec_hash, "take.spec_hash")
        if self.seed is not None and (
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
