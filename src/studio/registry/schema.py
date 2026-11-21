"""Registry schema: engines, model metadata and capability bindings.

Pure data. Model metadata is deliberately separate from model files: a ``ModelSpec`` only names files by a
logical locator (root key + relative path); absolute locations come from configuration and are resolved
by ``studio.registry.files``. Nothing in this module reads files or calls any service.

All models are frozen and use tuples (never lists) for collections, so a loaded registry is immutable
all the way down.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Final, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from studio.config.paths import PathValidationError, validate_relative_posix

# Operation, MediaType and CommercialUse are defined ONCE, in studio.domain.vocab (ADR-0010 P-6). The
# registry imports them (it may import the domain, never the reverse) and re-exports them from here.
from studio.domain.vocab import CommercialUse, MediaType, Operation
from studio.versions import SCHEMA_VERSION, SchemaVersion

# Registry ids: lowercase, segments separated by '.' or ':' (e.g. "comfyui", "comfyui.z_image_turbo.t2i").
RegistryId = Annotated[
    str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*(?:[.:][a-z0-9_]+)*$", max_length=96)
]
# Adapters are referenced by dotted name and NEVER imported by the registry. Only the studio's own
# engine package is accepted, so a registry file cannot name an arbitrary importable object.
AdapterName = Annotated[
    str,
    StringConstraints(
        pattern=r"^studio\.engines(?:\.[A-Za-z_][A-Za-z0-9_]*)+$",
        max_length=128,
    ),
]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
# Names of environment variables an executor sets for itself (BindingSpec.managed_environment).
EnvVarName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$", max_length=128)]
# One automatic-recovery limit: a non-negative integer, or None for "no automatic recovery of this kind".
RecoveryLimit = Annotated[int | None, Field(strict=True, ge=0)]
# The resume-chain cap: when set it is at least 1 (R-9); None = no resume-of-resume. 0 is rejected.
ResumeDepthLimit = Annotated[int | None, Field(strict=True, ge=1)]

# Parameter names the domain treats as first-class creative fields (they feed the creative identity).
# Mirrors studio.domain.production.RESERVED_CREATIVE_KEYS plus the design's payload names ``capability``
# and ``duration_ms``; kept registry-local so the registry does not depend on the domain's internals.
RESERVED_CREATIVE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "operation",
        "binding_id",
        "capability",
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
# The ONLY reserved creative fields a binding may list in ``execution_only`` (user decision I-1(b), UD-6:
# resolution and length may be adapted to resources). ``fps`` is never adaptable. Enforced by the loader.
EXECUTION_ONLY_CREATIVE_ALLOWLIST: Final[frozenset[str]] = frozenset(
    {"width", "height", "duration_s"}
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EngineKind(StrEnum):
    SERVICE = "service"  # resident, externally managed (ComfyUI)
    PROCESS = "process"  # ephemeral subprocess tree (LTX, workers)
    TOOL = "tool"  # media tool (FFmpeg)


class GpuClass(StrEnum):
    NONE = "none"
    EXCLUSIVE = "exclusive"
    MEDIA = (
        "media"  # NVENC/NVDEC/CUDA filters: real GPU work, needs the lease (architecture section 8)
    )


class EstimateBasis(StrEnum):
    """Where a resource number comes from. There is deliberately no 'guess' or 'placeholder' basis:
    a number nobody measured or observed is recorded as ``unknown`` (value null)."""

    UNKNOWN = "unknown"  # no value
    OBSERVED = "observed"  # one reported observation; not calibrated, does not generalize
    FILE_SIZE = "file_size"  # derived from model file sizes
    MEASURED = "measured"  # recorded by an authorized calibration run (M3.x)


class Determinism(StrEnum):
    BITWISE = "bitwise"
    PERCEPTUAL = "perceptual"
    NONE = "none"


class NegativePrompt(StrEnum):
    NONE = "none"
    EFFECTIVE = "effective"


class FrameRule(StrEnum):
    ANY = "any"
    EIGHT_K_PLUS_ONE = "8k+1"  # LTX: frames must be 8k+1

    def is_valid(self, frames: int) -> bool:
        if frames < 1:
            return False
        return self is FrameRule.ANY or (frames - 1) % 8 == 0


class FileRoot(StrEnum):
    """Logical root a model file is located under. Mapped to real paths by configuration."""

    MODELS = "models"  # PathsConfig.models_root
    LTX_MODELS = "ltx_models"  # PathsConfig.ltx_models


class ModelKind(StrEnum):
    DIFFUSION = "diffusion"
    TEXT_ENCODER = "text_encoder"
    VAE = "vae"
    VIDEO_PIPELINE = "video_pipeline"
    AUDIO = "audio"
    OTHER = "other"


class ProbeKind(StrEnum):
    COMFYUI_NODE_CLASS = "comfyui_node_class"  # must appear in /object_info (live probe, later)
    FFMPEG_ENCODER = "ffmpeg_encoder"  # must appear in `ffmpeg -encoders` (live probe, later)
    CLI_OPTION = "cli_option"  # option must exist in the tool's --help (live probe, later)


class License(_Strict):
    id: str = Field(min_length=1)
    commercial: CommercialUse = CommercialUse.UNKNOWN
    url: str | None = None
    notes: str = ""


class ModelFileRef(_Strict):
    root: FileRoot
    path: str = Field(
        min_length=1,
        description="relative, forward slashes; '.' means the whole root (directory models only)",
    )
    role: str = Field(min_length=1)
    size_bytes: int | None = Field(default=None, ge=0)
    sha256: Sha256 | None = None

    @field_validator("path")
    @classmethod
    def _relative(cls, v: str) -> str:
        try:
            return validate_relative_posix(v, allow_dot=True)
        except PathValidationError as exc:
            raise ValueError(f"model file path {v!r}: {exc}") from None


class ModelSpec(_Strict):
    """Metadata about a model. Contains no absolute paths and no model data."""

    schema_version: SchemaVersion = SCHEMA_VERSION
    id: RegistryId
    display_name: str = Field(min_length=1)
    kind: ModelKind
    family: str = Field(min_length=1)
    format: str = Field(min_length=1, description="gguf | safetensors | directory | ...")
    quantization: str | None = None
    license: License
    files: tuple[ModelFileRef, ...] = Field(min_length=1)
    notes: str = ""

    @model_validator(mode="after")
    def _dot_only_for_directories(self) -> ModelSpec:
        if self.format != "directory" and any(f.path == "." for f in self.files):
            raise ValueError(
                "path '.' (a whole root) is only valid for models with format 'directory'"
            )
        return self


class Quantity(_Strict):
    """One resource number with explicit provenance. ``value`` is ``None`` when nobody knows it.

    Unknown is a first-class state: it is never defaulted to a number, and for GPU memory it is never
    treated as statically compatible (``studio.registry.compat``).
    """

    value: float | None = Field(default=None, ge=0)
    basis: EstimateBasis = EstimateBasis.UNKNOWN
    source: str = Field(
        default="", description="who/what produced the value; required whenever value is set"
    )

    @model_validator(mode="after")
    def _provenance(self) -> Quantity:
        if self.value is None and self.basis is not EstimateBasis.UNKNOWN:
            raise ValueError("a null value must have basis 'unknown'")
        if self.value is not None:
            if self.basis is EstimateBasis.UNKNOWN:
                raise ValueError("a value needs a basis other than 'unknown'")
            if not self.source.strip():
                raise ValueError("a value needs a non-empty 'source' (provenance)")
        return self

    @property
    def known(self) -> bool:
        return self.value is not None


def _unknown_if_null(v: Any) -> Any:
    return {} if v is None else v


class ResourceProfile(_Strict):
    """Per-binding resource requirements consumed by the resource manager (ADR-0007).

    Every number is a ``Quantity``: ``null`` in YAML means unknown. ``estimate_includes_context`` says
    whether ``vram_gb`` already contains the engine's CUDA context (it drives ``new_context_term``); it
    may only be set once ``vram_gb`` is known, and ``None`` is treated as 'not included' (conservative).
    """

    gpu: GpuClass
    vram_gb: Quantity = Field(default_factory=Quantity)
    ram_gb: Quantity = Field(default_factory=Quantity)
    commit_gb: Quantity = Field(default_factory=Quantity)
    est_minutes: Quantity = Field(default_factory=Quantity)
    estimate_includes_context: bool | None = None
    notes: str = ""

    @field_validator("vram_gb", "ram_gb", "commit_gb", "est_minutes", mode="before")
    @classmethod
    def _null_means_unknown(cls, v: Any) -> Any:
        return _unknown_if_null(v)

    @model_validator(mode="after")
    def _consistent(self) -> ResourceProfile:
        if self.est_minutes.value is not None and self.est_minutes.value <= 0:
            raise ValueError("est_minutes must be > 0 when set")
        if self.estimate_includes_context is not None and not self.vram_gb.known:
            raise ValueError("estimate_includes_context requires a known vram_gb")
        if self.gpu is GpuClass.NONE and self.vram_gb.known and self.vram_gb.value != 0:
            raise ValueError("gpu class 'none' must not declare a non-zero vram_gb")
        if self.gpu is not GpuClass.NONE and self.vram_gb.value == 0:
            raise ValueError("a GPU binding must not declare vram_gb = 0; leave it unknown instead")
        return self


class IoSlot(_Strict):
    role: str = Field(min_length=1)
    media_type: MediaType
    required: bool = True


class Constraints(_Strict):
    dim_multiple: int | None = Field(default=None, ge=1)
    frame_rule: FrameRule = FrameRule.ANY
    min_width: int | None = Field(default=None, ge=1)
    max_width: int | None = Field(default=None, ge=1)
    min_height: int | None = Field(default=None, ge=1)
    max_height: int | None = Field(default=None, ge=1)
    max_frames: int | None = Field(default=None, ge=1)
    fps: tuple[float, ...] = ()
    max_prompt_tokens: int | None = Field(default=None, ge=1)


class Features(_Strict):
    negative_prompt: NegativePrompt = NegativePrompt.NONE
    native_audio: bool = False
    mask: bool = False
    reference_images: bool = False
    resume: bool = False
    preview: bool = False


class Probe(_Strict):
    kind: ProbeKind
    value: str = Field(min_length=1)


class RecoveryPolicy(_Strict):
    """Automatic-recovery limits of an engine (default) or of one binding (override).

    ``None`` means no automatic recovery of that kind. There is deliberately NO default number
    (UD-4, UD-7). A binding block REPLACES the engine block as a whole; fields are never merged
    (``Registry.resolve_recovery``). The field names are shared with the domain's policy snapshot.
    """

    max_infrastructure_retries: RecoveryLimit = None
    max_resumes: RecoveryLimit = None
    max_resume_depth: ResumeDepthLimit = Field(
        default=None,
        description="cap on a chain of consecutive resumes; None = no resume-of-resume",
    )


class EngineSpec(_Strict):
    """Identifies a backend and what it can do in principle. Connection details live in configuration."""

    schema_version: SchemaVersion = SCHEMA_VERSION
    id: RegistryId
    display_name: str = Field(min_length=1)
    kind: EngineKind
    adapter: AdapterName = Field(
        description="dotted name of the adapter class under studio.engines; never imported here"
    )
    operations: tuple[Operation, ...] = Field(min_length=1)
    probes: tuple[Probe, ...] = ()
    recovery: RecoveryPolicy | None = Field(
        default=None, description="default recovery limits for the engine's bindings; absent = none"
    )
    notes: str = ""


class TemplateRef(_Strict):
    """Placeholder for a workflow template. Template resolution (paths, hashes, existence) is deferred
    to milestone M2.1; until then a template can only be declared as explicitly unresolved, so the
    registry never contains a path that looks real but points at nothing."""

    status: Literal["unresolved"] = (
        "unresolved"  # M2.1 adds "resolved" together with real resolution
    )
    hint: str | None = Field(default=None, min_length=1, description="free-text intent, not a path")


class BindingSpec(_Strict):
    """One capability: engine + models + template/preset for exactly one operation."""

    schema_version: SchemaVersion = SCHEMA_VERSION
    id: RegistryId
    display_name: str = Field(min_length=1)
    engine: RegistryId
    operation: Operation
    models: tuple[RegistryId, ...] = ()
    inputs: tuple[IoSlot, ...] = ()
    outputs: tuple[IoSlot, ...] = Field(min_length=1)
    constraints: Constraints = Field(default_factory=Constraints)
    features: Features = Field(default_factory=Features)
    determinism: Determinism = Determinism.NONE
    execution_only: tuple[str, ...] = Field(
        default=(),
        description="parameter keys excluded from spec_hash (architecture section 3)",
    )
    managed_environment: tuple[EnvVarName, ...] = Field(
        default=(),
        description="environment variables the executor sets itself: never task parameters, never "
        "hashed, and never also listed in execution_only",
    )
    recovery: RecoveryPolicy | None = Field(
        default=None,
        description="replaces the engine's recovery block as a whole; absent = the engine's block",
    )
    resources: ResourceProfile
    template: TemplateRef | None = None
    notes: str = ""
