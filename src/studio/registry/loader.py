"""Registry loading and validation. Reads YAML files only; never calls any service."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel, ValidationError
from ruamel.yaml import YAML

from studio.config.checks import is_reparse_point
from studio.registry.schema import (
    EXECUTION_ONLY_CREATIVE_ALLOWLIST,
    RESERVED_CREATIVE_FIELDS,
    BindingSpec,
    Determinism,
    EngineKind,
    EngineSpec,
    GpuClass,
    MediaType,
    ModelSpec,
    Operation,
)

Severity = Literal["error", "warning"]

YAML_SUFFIXES = (".yaml", ".yml")  # both are accepted; the suffix carries no meaning
MAX_REGISTRY_FILE_BYTES = 1_000_000


@dataclass(frozen=True)
class RegistryIssue:
    severity: Severity
    code: str
    subject: str
    message: str

    def __str__(self) -> str:
        return f"[{self.severity}] {self.code} {self.subject}: {self.message}"


class RegistryError(Exception):
    def __init__(self, issues: list[RegistryIssue]) -> None:
        self.issues = issues
        super().__init__("\n".join(str(i) for i in issues))


@dataclass(frozen=True)
class ResolvedRecovery:
    """The recovery limits that apply to one binding, and where they came from.

    ``source`` is ``"binding:<id>"``, ``"engine:<id>"`` or ``"none"`` (no automatic recovery). A limit of
    ``None`` means none of that kind. A later milestone builds the domain's policy snapshot from this.
    """

    source: str
    max_infrastructure_retries: int | None = None
    max_resumes: int | None = None
    max_resume_depth: int | None = None


@dataclass(frozen=True, eq=False)  # identity equality/hash: the read-only mappings are not hashable
class Registry:
    engines: Mapping[str, EngineSpec]
    models: Mapping[str, ModelSpec]
    bindings: Mapping[str, BindingSpec]
    warnings: tuple[RegistryIssue, ...] = field(default=())

    def __post_init__(self) -> None:
        # Read-only views: a loaded registry cannot be mutated through its mappings.
        for name in ("engines", "models", "bindings"):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))

    def bindings_for(self, operation: Operation) -> list[BindingSpec]:
        return [b for b in self.bindings.values() if b.operation is operation]

    def bindings_of_engine(self, engine_id: str) -> list[BindingSpec]:
        return [b for b in self.bindings.values() if b.engine == engine_id]

    def resolve_recovery(self, binding_id: str) -> ResolvedRecovery:
        """A binding's ``recovery`` block replaces the engine's as a whole (no field merging); with
        neither block there is no automatic recovery. Unknown ids raise ``KeyError``."""
        binding = self.bindings[binding_id]
        engine = self.engines.get(binding.engine)
        if binding.recovery is not None:
            source, block = f"binding:{binding.id}", binding.recovery
        elif engine is not None and engine.recovery is not None:
            source, block = f"engine:{engine.id}", engine.recovery
        else:
            return ResolvedRecovery(source="none")
        return ResolvedRecovery(
            source=source,
            max_infrastructure_retries=block.max_infrastructure_retries,
            max_resumes=block.max_resumes,
            max_resume_depth=block.max_resume_depth,
        )


_SECTIONS: tuple[tuple[str, type[BaseModel]], ...] = (
    ("engines", EngineSpec),
    ("models", ModelSpec),
    ("bindings", BindingSpec),
)

# Required input roles per operation (a binding must declare them as required inputs).
_REQUIRED_INPUTS: dict[Operation, dict[str, MediaType]] = {
    Operation.I2I: {"init_image": MediaType.IMAGE},
    Operation.INPAINT: {"init_image": MediaType.IMAGE, "mask": MediaType.MASK},
    Operation.I2V: {"start_frame": MediaType.IMAGE},
    Operation.V2V: {"clip": MediaType.VIDEO},
    Operation.UPSCALE: {"clip": MediaType.VIDEO},
    Operation.INTERP: {"clip": MediaType.VIDEO},
    Operation.TRANSCODE: {"clip": MediaType.VIDEO},
    Operation.ASR: {"audio": MediaType.AUDIO},
    Operation.RENDER_TIMELINE: {"timeline": MediaType.TIMELINE},
}

_I, _V, _A, _T, _M, _TL = (
    MediaType.IMAGE,
    MediaType.VIDEO,
    MediaType.AUDIO,
    MediaType.TEXT,
    MediaType.MASK,
    MediaType.TIMELINE,
)
# PROVISIONAL compatibility policy (M0.2): which media types an operation may take / produce.
# Widen deliberately, with a reason, when a real engine needs it (proposal P-5 in ADR-0008, awaiting approval).
_ALLOWED_INPUTS: dict[Operation, frozenset[MediaType]] = {
    Operation.T2I: frozenset({_I}),  # reference images
    Operation.I2I: frozenset({_I}),
    Operation.INPAINT: frozenset({_I, _M}),
    Operation.T2V: frozenset({_I, _A}),
    Operation.I2V: frozenset({_I, _A}),
    Operation.V2V: frozenset({_V, _I, _A}),
    Operation.UPSCALE: frozenset({_V}),
    Operation.INTERP: frozenset({_V}),
    Operation.TTS: frozenset({_A, _T}),
    Operation.MUSIC: frozenset({_A, _T}),
    Operation.SFX: frozenset({_A, _T}),
    Operation.ASR: frozenset({_A}),
    Operation.TRANSCODE: frozenset({_V, _A}),
    Operation.RENDER_TIMELINE: frozenset({_TL, _V, _A, _I}),
}
_ALLOWED_OUTPUTS: dict[Operation, frozenset[MediaType]] = {
    Operation.T2I: frozenset({_I}),
    Operation.I2I: frozenset({_I}),
    Operation.INPAINT: frozenset({_I}),
    Operation.T2V: frozenset({_V, _A}),
    Operation.I2V: frozenset({_V, _A}),
    Operation.V2V: frozenset({_V, _A}),
    Operation.UPSCALE: frozenset({_V}),
    Operation.INTERP: frozenset({_V}),
    Operation.TTS: frozenset({_A}),
    Operation.MUSIC: frozenset({_A}),
    Operation.SFX: frozenset({_A}),
    Operation.ASR: frozenset({_T}),
    Operation.TRANSCODE: frozenset({_V, _A}),
    Operation.RENDER_TIMELINE: frozenset({_V}),
}


def _list_registry_files(folder: Path, section: str, issues: list[RegistryIssue]) -> list[Path]:
    files: list[Path] = []
    for entry in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
        if entry.suffix.lower() not in YAML_SUFFIXES:
            if entry.is_file():
                issues.append(
                    RegistryIssue(
                        "warning",
                        "ignored_file",
                        f"{section}/{entry.name}",
                        "not a .yaml/.yml file",
                    )
                )
            else:
                issues.append(
                    RegistryIssue(
                        "warning",
                        "ignored_entry",
                        f"{section}/{entry.name}",
                        "not a regular file (registry folders are not searched recursively)",
                    )
                )
            continue
        if is_reparse_point(entry):
            issues.append(
                RegistryIssue(
                    "error",
                    "link_rejected",
                    f"{section}/{entry.name}",
                    "registry files must be regular files, not symlinks/junctions",
                )
            )
            continue
        if not entry.is_file():
            issues.append(
                RegistryIssue(
                    "warning",
                    "ignored_entry",
                    f"{section}/{entry.name}",
                    "has a YAML name but is not a regular file",
                )
            )
            continue
        if entry.stat().st_size > MAX_REGISTRY_FILE_BYTES:
            issues.append(
                RegistryIssue(
                    "error",
                    "file_too_large",
                    f"{section}/{entry.name}",
                    "exceeds registry file limit",
                )
            )
            continue
        files.append(entry)
    return files


def load_registry(root: Path) -> Registry:
    """Load ``<root>/{engines,models,bindings}/*.{yaml,yml}``; raise ``RegistryError`` listing every error.

    An empty registry (no engines or no bindings) is an error.
    """
    issues: list[RegistryIssue] = []
    found: dict[str, dict[str, BaseModel]] = {name: {} for name, _ in _SECTIONS}
    origin: dict[tuple[str, str], Path] = {}
    yaml = YAML(typ="safe")  # duplicate mapping keys raise; no arbitrary object construction

    for section, model in _SECTIONS:
        folder = root / section
        if is_reparse_point(folder):
            issues.append(
                RegistryIssue(
                    "error", "link_rejected", section, f"folder is a symlink/junction: {folder}"
                )
            )
            continue
        if not folder.is_dir():
            issues.append(RegistryIssue("error", "missing_dir", section, f"not found: {folder}"))
            continue
        for file in _list_registry_files(folder, section, issues):
            try:
                data = yaml.load(file)
            except Exception as exc:  # noqa: BLE001 - report any parser failure with its file
                issues.append(RegistryIssue("error", "yaml_error", str(file), str(exc)))
                continue
            if not isinstance(data, dict):
                issues.append(
                    RegistryIssue(
                        "error", "not_a_mapping", str(file), "top level must be a mapping"
                    )
                )
                continue
            try:
                obj = model.model_validate(data)
            except ValidationError as exc:
                for err in exc.errors():
                    loc = ".".join(str(x) for x in err["loc"])
                    issues.append(
                        RegistryIssue("error", "schema", f"{file.name}:{loc}", err["msg"])
                    )
                continue
            obj_id = getattr(obj, "id")  # noqa: B009 - all three specs define ``id``
            if obj_id in found[section]:
                issues.append(
                    RegistryIssue(
                        "error",
                        "duplicate_id",
                        obj_id,
                        f"defined in {origin[(section, obj_id)].name} and {file.name}",
                    )
                )
                continue
            if file.stem != obj_id.replace(":", "_"):
                issues.append(
                    RegistryIssue(
                        "warning", "filename", file.name, f"file name does not match id {obj_id!r}"
                    )
                )
            found[section][obj_id] = obj
            origin[(section, obj_id)] = file

    registry = Registry(
        engines={k: v for k, v in found["engines"].items() if isinstance(v, EngineSpec)},
        models={k: v for k, v in found["models"].items() if isinstance(v, ModelSpec)},
        bindings={k: v for k, v in found["bindings"].items() if isinstance(v, BindingSpec)},
    )
    issues.extend(validate_registry(registry))
    if any(i.severity == "error" for i in issues):
        raise RegistryError(issues)
    return Registry(
        engines=registry.engines,
        models=registry.models,
        bindings=registry.bindings,
        warnings=tuple(issues),
    )


def validate_registry(reg: Registry) -> list[RegistryIssue]:
    """Cross-reference and consistency checks over already-parsed specs. Pure.

    Hardware-independent. Whether a binding fits a machine is ``studio.registry.compat``.
    """
    out: list[RegistryIssue] = []

    def err(code: str, subject: str, msg: str) -> None:
        out.append(RegistryIssue("error", code, subject, msg))

    def warn(code: str, subject: str, msg: str) -> None:
        out.append(RegistryIssue("warning", code, subject, msg))

    if not reg.engines:
        err("empty_registry", "engines", "the registry defines no engines")
    if not reg.bindings:
        err("empty_registry", "bindings", "the registry defines no bindings")

    for b in reg.bindings.values():
        engine = reg.engines.get(b.engine)
        if engine is None:
            err("unknown_engine", b.id, f"engine {b.engine!r} is not defined")
        else:
            if b.operation not in engine.operations:
                err(
                    "operation_unsupported",
                    b.id,
                    f"engine {engine.id!r} does not declare operation {b.operation.value!r}",
                )
            if engine.kind is EngineKind.TOOL and b.resources.gpu is GpuClass.EXCLUSIVE:
                err(
                    "gpu_class",
                    b.id,
                    "a media tool must use gpu 'none' or 'media', not 'exclusive'",
                )
            if engine.kind is not EngineKind.TOOL and b.resources.gpu is GpuClass.MEDIA:
                err("gpu_class", b.id, "gpu class 'media' is only valid for media tools")
        for m in b.models:
            if m not in reg.models:
                err("unknown_model", b.id, f"model {m!r} is not defined")
        for label, qty, applies in (
            ("vram", b.resources.vram_gb, b.resources.gpu is not GpuClass.NONE),
            ("ram", b.resources.ram_gb, True),
            ("commit", b.resources.commit_gb, True),
        ):
            if applies and not qty.known:
                warn(
                    f"{label}_unknown",
                    b.id,
                    f"{label} requirement is unknown; the binding is NOT statically compatible until it is measured",
                )
        roles = [s.role for s in b.inputs]
        if len(roles) != len(set(roles)):
            err("duplicate_role", b.id, "input roles must be unique")
        out_roles = [s.role for s in b.outputs]
        if len(out_roles) != len(set(out_roles)):
            err("duplicate_role", b.id, "output roles must be unique")
        for role, media in _REQUIRED_INPUTS.get(b.operation, {}).items():
            slot = next((s for s in b.inputs if s.role == role), None)
            if slot is None or not slot.required or slot.media_type is not media:
                err(
                    "missing_input",
                    b.id,
                    f"operation {b.operation.value!r} requires a required input {role!r} of type {media.value!r}",
                )
        allowed_in = _ALLOWED_INPUTS[b.operation]
        for s in b.inputs:
            if s.media_type not in allowed_in:
                err(
                    "input_incompatible",
                    b.id,
                    f"input {s.role!r} of type {s.media_type.value!r} is not valid for "
                    f"operation {b.operation.value!r}",
                )
        allowed_out = _ALLOWED_OUTPUTS[b.operation]
        for s in b.outputs:
            if s.media_type not in allowed_out:
                err(
                    "output_incompatible",
                    b.id,
                    f"output {s.role!r} of type {s.media_type.value!r} is not valid for "
                    f"operation {b.operation.value!r}",
                )
        if not any(s.required for s in b.outputs):
            err("no_required_output", b.id, "at least one output must be required")
        if b.features.mask and not any(s.media_type is MediaType.MASK for s in b.inputs):
            err("feature_mismatch", b.id, "features.mask is true but no mask input is declared")
        if b.features.reference_images and not any(
            s.media_type is MediaType.IMAGE for s in b.inputs
        ):
            err("feature_mismatch", b.id, "features.reference_images needs an image input")
        if b.features.native_audio and not any(s.media_type is MediaType.VIDEO for s in b.outputs):
            err("feature_mismatch", b.id, "features.native_audio needs a video output")
        if len(set(b.execution_only)) != len(b.execution_only):
            err("duplicate_key", b.id, "execution_only keys must be unique")
        managed = [name.casefold() for name in b.managed_environment]  # Windows names: case-blind
        if len(set(managed)) != len(managed):
            err("duplicate_key", b.id, "managed_environment names must be unique")
        shared = sorted({key.casefold() for key in b.execution_only} & set(managed))
        if shared:
            err(
                "managed_environment_conflict",
                b.id,
                f"{shared} appear in both execution_only and managed_environment; a managed "
                "environment variable is set by the executor and is never a task parameter",
            )
        for key in b.execution_only:
            if key in RESERVED_CREATIVE_FIELDS and key not in EXECUTION_ONLY_CREATIVE_ALLOWLIST:
                err(
                    "execution_only_reserved",
                    b.id,
                    f"execution_only may not name the creative field {key!r}; only "
                    f"{sorted(EXECUTION_ONLY_CREATIVE_ALLOWLIST)} may be adapted",
                )
        if b.determinism is Determinism.BITWISE and b.execution_only:
            warn(
                "determinism_execution_only",
                b.id,
                "determinism 'bitwise' is combined with execution-only parameters, which may change "
                "the output bits (for example thread counts); this cannot be verified without "
                "execution, so declare 'perceptual' unless proven",
            )
        c = b.constraints
        for lo, hi, label in (
            (c.min_width, c.max_width, "width"),
            (c.min_height, c.max_height, "height"),
        ):
            if lo is not None and hi is not None and lo > hi:
                err("constraint_range", b.id, f"min_{label} > max_{label}")
        if b.template is not None:
            warn("template_unresolved", b.id, "workflow template is unresolved until M2.1")

    for e in reg.engines.values():
        if len(set(e.operations)) != len(e.operations):
            err("duplicate_operation", e.id, "operations must be unique")
        if not reg.bindings_of_engine(e.id):
            warn("no_bindings", e.id, "engine has no bindings")

    for m in reg.models.values():
        keys = [(f.root, f.path.casefold()) for f in m.files]
        if len(set(keys)) != len(keys):
            err("duplicate_file", m.id, "a model lists the same file twice")
        if not any(m.id in b.models for b in reg.bindings.values()):
            warn("unused_model", m.id, "model is not referenced by any binding")
    return out
