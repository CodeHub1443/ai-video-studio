"""Asset domain: identity, content hash, versions, lineage, provenance, rights, immutable object reference.

Concept map (docs/architecture.md section 6, ADR-0006):
* ``ObjectRef``     an immutable, content-addressed blob, identified only by SHA-256 and size. No path.
* ``Asset``         a logical thing ("the hero bottle render").
* ``AssetVersion``  an immutable revision of an asset pointing at one object.
* ``LineageEdge``   append-only parent/child relation between versions.
* ``AssetRef``      how production and workflow specs point at assets (optionally pinned to a version).

Not here: Slot/SlotBinding (a later milestone), object-store layout, ingestion, hashing, file access.
Where an object lives on disk is a persistence concern and is deliberately not derivable from this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from studio.domain.canonical import ParamMap, freeze_params
from studio.domain.errors import ValidationIssue, UnresolvedSpecError, fail
from studio.domain.graph import topological_order
from studio.domain.ids import (
    ROLE_RE,
    AssetId,
    AttemptId,
    JobId,
    ProjectId,
    VersionId,
    check_id,
    check_optional_id,
    check_registry_id,
    check_sha256,
    check_text,
)
from studio.domain.states import AssetState
from studio.domain.vocab import CommercialUse, MediaType

HASH_ALGORITHM = "sha256"


def _set(obj: object, name: str, value: Any) -> None:
    object.__setattr__(obj, name, value)


def _check_aware(value: datetime | None, path: str) -> None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        fail("time.naive", path, "timestamps must be timezone-aware")


@dataclass(frozen=True, kw_only=True)
class ObjectRef:
    """Immutable reference to a content-addressed object. Identity is the digest; ``size_bytes`` is a
    second integrity fact (a mismatch with the same digest is corruption, never a new object)."""

    sha256: str
    size_bytes: int

    def __post_init__(self) -> None:
        check_sha256(self.sha256, "object.sha256")
        if (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or self.size_bytes < 0
        ):
            fail("object.size", "object.size_bytes", "size must be a non-negative integer")

    @property
    def key(self) -> str:
        """Algorithm-qualified identity, e.g. ``sha256:ab12...``."""
        return f"{HASH_ALGORITHM}:{self.sha256}"


@dataclass(frozen=True, kw_only=True)
class MediaMetadata:
    """Probe results. Every field is optional because a probe may not know it; relevance per media type is
    checked by ``check_metadata_for``. Unknown stays ``None``; nothing is defaulted."""

    container: str | None = None
    codec: str | None = None
    mime_type: str | None = None
    width: int | None = None
    height: int | None = None
    duration_s: float | None = None
    fps: float | None = None
    frame_count: int | None = None
    audio_channels: int | None = None
    sample_rate_hz: int | None = None
    extra: ParamMap = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("container", "codec", "mime_type"):
            value = getattr(self, name)
            if value is not None:
                check_text(value, f"metadata.{name}")
        for name in ("width", "height", "frame_count", "audio_channels", "sample_rate_hz"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 1
            ):
                fail("metadata.range", f"metadata.{name}", "must be an integer >= 1")
        for name in ("duration_s", "fps"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0
            ):
                fail("metadata.range", f"metadata.{name}", "must be a number > 0")
        _set(self, "extra", freeze_params(self.extra, "metadata.extra"))


_VISUAL = frozenset({MediaType.IMAGE, MediaType.VIDEO, MediaType.MASK})


def check_metadata_for(
    media_type: MediaType, metadata: MediaMetadata, path: str = "metadata"
) -> tuple[ValidationIssue, ...]:
    """Structural plausibility of probe metadata for a media type. Missing values are allowed (unknown);
    contradictory or irrelevant values are not."""
    issues: list[ValidationIssue] = []

    def bad(code: str, message: str) -> None:
        issues.append(ValidationIssue(code, path, message))

    if media_type in _VISUAL:
        if (metadata.width is None) != (metadata.height is None):
            bad("metadata.size_pair", "width and height must be given together")
    elif metadata.width is not None or metadata.height is not None:
        bad("metadata.irrelevant", f"width/height do not apply to {media_type}")
    if media_type is MediaType.IMAGE or media_type is MediaType.MASK:
        for name in ("duration_s", "fps", "frame_count"):
            if getattr(metadata, name) is not None:
                bad("metadata.irrelevant", f"{name} does not apply to {media_type}")
    if media_type is MediaType.AUDIO and (
        metadata.fps is not None or metadata.frame_count is not None
    ):
        bad("metadata.irrelevant", "fps/frame_count do not apply to audio")
    if media_type in (MediaType.IMAGE, MediaType.MASK, MediaType.TEXT, MediaType.TIMELINE) and (
        metadata.audio_channels is not None or metadata.sample_rate_hz is not None
    ):
        bad("metadata.irrelevant", f"audio fields do not apply to {media_type}")
    return tuple(issues)


class ProvenanceOrigin(StrEnum):
    GENERATED = "generated"  # produced by a job from non-asset inputs (prompt, seed, ...)
    DERIVED = "derived"  # produced by a job by transforming other assets (needs lineage parents)
    IMPORTED = "imported"  # entered from outside the studio (user original, library file)


@dataclass(frozen=True, kw_only=True)
class Provenance:
    """Where a version came from. For produced versions it names the job/attempt and the capability used,
    never an engine internals."""

    origin: ProvenanceOrigin
    binding_id: str | None = None  # registry binding id of the capability that produced it
    job_id: JobId | None = None
    attempt_id: AttemptId | None = None
    spec_hash: str | None = None  # the producing task's spec_hash, if known
    source_description: str | None = None  # required for IMPORTED: where the bytes came from
    original_name: str | None = None  # file name at the source, informational only

    def __post_init__(self) -> None:
        if self.binding_id is not None:
            check_registry_id(self.binding_id, "provenance.binding_id")
        check_optional_id(self.job_id, "provenance.job_id")
        check_optional_id(self.attempt_id, "provenance.attempt_id")
        if self.spec_hash is not None:
            check_sha256(self.spec_hash, "provenance.spec_hash")
        if self.source_description is not None:
            check_text(self.source_description, "provenance.source_description")
        if self.original_name is not None:
            check_text(self.original_name, "provenance.original_name")
        produced = self.origin is not ProvenanceOrigin.IMPORTED
        if produced:
            missing = [
                n
                for n, v in (
                    ("binding_id", self.binding_id),
                    ("job_id", self.job_id),
                    ("attempt_id", self.attempt_id),
                )
                if v is None
            ]
            if missing:
                fail(
                    "provenance.incomplete",
                    "provenance",
                    f"{self.origin} needs {', '.join(missing)}",
                )
        else:
            if any(
                v is not None
                for v in (self.binding_id, self.job_id, self.attempt_id, self.spec_hash)
            ):
                fail(
                    "provenance.imported_producer",
                    "provenance",
                    "imported assets have no producing job",
                )
            if self.source_description is None:
                fail(
                    "provenance.source_missing",
                    "provenance",
                    "imported assets need a source_description",
                )


@dataclass(frozen=True, kw_only=True)
class RightsInfo:
    """Recorded rights facts. This is *metadata only*: nothing here decides whether an asset may be used or
    exported. The default is "all unknown". Which values block which use is an undecided policy (D7-D9)."""

    license_id: str | None = None
    rights_holder: str | None = None
    commercial_use: CommercialUse = CommercialUse.UNKNOWN
    attribution_required: bool | None = None  # None = unknown
    terms_url: str | None = None  # a recorded string; never fetched
    restrictions: tuple[str, ...] = ()
    notes: str = ""

    def __post_init__(self) -> None:
        for name in ("license_id", "rights_holder", "terms_url"):
            value = getattr(self, name)
            if value is not None:
                check_text(value, f"rights.{name}")
        _set(
            self,
            "restrictions",
            tuple(check_text(r, "rights.restrictions") for r in self.restrictions),
        )
        check_text(self.notes, "rights.notes", allow_empty=True)

    @property
    def is_unknown(self) -> bool:
        return (
            self.license_id is None
            and self.rights_holder is None
            and self.commercial_use is CommercialUse.UNKNOWN
            and self.attribution_required is None
        )


@dataclass(frozen=True, kw_only=True)
class Asset:
    """A logical asset. Immutable identity; its content lives in its versions."""

    asset_id: AssetId
    project_id: ProjectId
    media_type: MediaType
    name: str

    def __post_init__(self) -> None:
        check_id(self.asset_id, "asset.asset_id")
        check_id(self.project_id, "asset.project_id")
        check_text(self.name, "asset.name")


@dataclass(frozen=True, kw_only=True)
class AssetVersion:
    """One immutable revision. Only ``state`` (and nothing about the bytes) changes over time."""

    version_id: VersionId
    asset_id: AssetId
    version_number: int
    state: AssetState
    object_ref: ObjectRef
    metadata: MediaMetadata = field(default_factory=MediaMetadata)
    provenance: Provenance
    rights: RightsInfo = field(default_factory=RightsInfo)
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        check_id(self.version_id, "version.version_id")
        check_id(self.asset_id, "version.asset_id")
        if (
            isinstance(self.version_number, bool)
            or not isinstance(self.version_number, int)
            or self.version_number < 1
        ):
            fail("version.number", "version.version_number", "must be an integer >= 1")
        _check_aware(self.created_at, "version.created_at")


class LineageRelation(StrEnum):
    INPUT = "input"  # parent was an input/conditioning asset of the job that produced the child
    TRANSFORM = "transform"  # child is the parent transformed (upscale, transcode, ...)
    RESUME_FROM = "resume_from"  # child continues from parent, an intermediate (stage-1) artifact


@dataclass(frozen=True, kw_only=True)
class LineageEdge:
    parent_version_id: VersionId
    child_version_id: VersionId
    relation: LineageRelation
    role: str | None = None  # the input role the parent played, when applicable

    def __post_init__(self) -> None:
        check_id(self.parent_version_id, "lineage.parent_version_id")
        check_id(self.child_version_id, "lineage.child_version_id")
        if self.parent_version_id == self.child_version_id:
            fail("lineage.self_edge", "lineage", "a version cannot descend from itself")
        if self.role is not None:
            check_id(self.role, "lineage.role", ROLE_RE)


def validate_lineage(
    edges: tuple[LineageEdge, ...] | list[LineageEdge],
) -> tuple[ValidationIssue, ...]:
    """Duplicate and cycle checks over lineage edges alone (endpoint existence is a snapshot rule)."""
    issues: list[ValidationIssue] = []
    seen: set[LineageEdge] = set()
    for edge in edges:
        if edge in seen:
            issues.append(
                ValidationIssue(
                    "lineage.duplicate",
                    f"lineage[{edge.parent_version_id}->{edge.child_version_id}]",
                    "duplicate edge",
                )
            )
        seen.add(edge)
    nodes = {e.parent_version_id for e in edges} | {e.child_version_id for e in edges}
    _, remaining = topological_order(
        nodes, ((e.parent_version_id, e.child_version_id) for e in edges)
    )
    if remaining:
        issues.append(
            ValidationIssue(
                "lineage.cycle", "lineage", f"cycle among versions: {', '.join(remaining)}"
            )
        )
    return tuple(issues)


@dataclass(frozen=True, kw_only=True)
class AssetRef:
    """A reference from a spec to an asset.

    * *authored*: ``asset_id`` only (latest-version intent) or ``asset_id`` + ``version_id``;
    * *resolved*: ``version_id`` and ``sha256`` both set. Only resolved refs may enter a fingerprint,
      because content, not a database id, is what makes two tasks equal.
    """

    role: str
    asset_id: AssetId
    version_id: VersionId | None = None
    sha256: str | None = None

    def __post_init__(self) -> None:
        check_id(self.role, "asset_ref.role", ROLE_RE)
        check_id(self.asset_id, "asset_ref.asset_id")
        check_optional_id(self.version_id, "asset_ref.version_id")
        if self.sha256 is not None:
            check_sha256(self.sha256, "asset_ref.sha256")
            if self.version_id is None:
                fail(
                    "asset_ref.unpinned_hash",
                    "asset_ref",
                    "a content hash requires a pinned version_id",
                )

    @property
    def is_resolved(self) -> bool:
        return self.version_id is not None and self.sha256 is not None

    def canonical_form(self, *, resolved: bool) -> dict[str, Any]:
        if resolved:
            if not self.is_resolved:
                raise UnresolvedSpecError(
                    f"asset reference for role {self.role!r} is not pinned to content"
                )
            return {"role": self.role, "sha256": self.sha256}
        return {"role": self.role, "asset_id": self.asset_id, "version_id": self.version_id}
