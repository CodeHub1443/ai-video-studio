"""Resolve logical model-file locators to real paths, and (optionally) check they exist.

Keeps registry metadata separate from the actual model files: only this module joins registry data with
configured roots. ``resolve_model_file`` is pure and lexical; ``check_model_files`` only stats paths
(read-only). Neither follows nor detects symlinks/junctions on its own: a link inside a models folder is
a documented residual risk (docs/security-model.md); ``check_model_files`` reports it as a warning.
"""

from __future__ import annotations

from pathlib import Path

from studio.config.checks import is_reparse_point
from studio.config.paths import PathValidationError, is_within, validate_relative_posix
from studio.config.settings import PathsConfig
from studio.registry.loader import Registry, RegistryIssue
from studio.registry.schema import FileRoot, ModelFileRef


def root_path(root: FileRoot, paths: PathsConfig) -> Path:
    if root is FileRoot.MODELS:
        return paths.models_root
    if root is FileRoot.LTX_MODELS:
        return paths.ltx_models
    raise ValueError(f"unknown file root {root!r}")  # pragma: no cover


def resolve_model_file(ref: ModelFileRef, paths: PathsConfig) -> Path:
    base = root_path(ref.root, paths)
    try:
        validate_relative_posix(ref.path, allow_dot=True)  # defence in depth: schema checks it too
    except PathValidationError as exc:
        raise ValueError(f"invalid model file path {ref.path!r}: {exc}") from None
    target = base if ref.path == "." else base.joinpath(*ref.path.split("/"))
    if target != base and not is_within(target, base):
        raise ValueError(f"model file escapes its root: {ref.path}")
    return target


def check_model_files(registry: Registry, paths: PathsConfig) -> list[RegistryIssue]:
    """Read-only existence and size check. Never reads file contents or computes hashes."""
    issues: list[RegistryIssue] = []
    for model in registry.models.values():
        for ref in model.files:
            target = resolve_model_file(ref, paths)
            if not target.exists():
                issues.append(RegistryIssue("error", "model_file_missing", model.id, str(target)))
                continue
            if target != root_path(ref.root, paths) and is_reparse_point(target):
                issues.append(
                    RegistryIssue(
                        "warning",
                        "model_file_link",
                        model.id,
                        f"{target} is a symlink/junction; its real location is not verified",
                    )
                )
            if ref.size_bytes is not None and target.is_file():
                actual = target.stat().st_size
                if actual != ref.size_bytes:
                    issues.append(
                        RegistryIssue(
                            "error",
                            "model_file_size",
                            model.id,
                            f"{target}: expected {ref.size_bytes} bytes, found {actual}",
                        )
                    )
    return issues
