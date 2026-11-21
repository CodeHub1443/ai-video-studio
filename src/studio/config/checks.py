"""Read-only environment checks. Separate from settings construction on purpose.

These functions only stat paths (``lstat``/``exists``). They never create, write or delete anything, and
never call ComfyUI, LTX, NVML or any network service. Nothing runs at import or construction time.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Literal

from studio.config.paths import is_within
from studio.config.settings import ConfigDirNotFound, StudioSettings, config_dir

Severity = Literal["error", "warning", "info"]

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


@dataclass(frozen=True)
class ConfigIssue:
    severity: Severity
    code: str
    subject: str
    message: str


def probe_reparse_point(path: Path) -> bool | None:
    """Tri-state link check: True for a symlink/junction/reparse point, False for a regular entry or a
    path that does not exist (``FileNotFoundError``, ``NotADirectoryError``), and ``None`` (unknown)
    when ``lstat`` fails for any other reason. Guard callers must treat ``None`` as a denial.
    """
    try:
        st = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except (OSError, ValueError):
        return None
    attrs = getattr(st, "st_file_attributes", 0)
    return stat.S_ISLNK(st.st_mode) or bool(attrs & _FILE_ATTRIBUTE_REPARSE_POINT)


def is_reparse_point(path: Path) -> bool:
    """True for symlinks, junctions and other reparse points (OneDrive placeholders, mount points).

    Uses ``lstat`` so the link itself is examined, not its target. A missing path is not a link.
    FAIL-OPEN for non-guard callers: an unknown result (``probe_reparse_point`` is ``None``) is reported
    as False here, exactly as before. Code that must fail closed uses ``probe_reparse_point``.
    """
    return bool(probe_reparse_point(path))


def find_reparse_points(path: Path) -> list[Path]:
    """``path`` and each of its existing ancestors that is a reparse point (read-only)."""
    pw = PureWindowsPath(str(path))
    found: list[Path] = []
    for i in range(len(pw.parts), 1, -1):  # skip the bare drive anchor
        candidate = Path(*pw.parts[:i])
        if is_reparse_point(candidate):
            found.append(candidate)
    return found


def check_environment(settings: StudioSettings) -> list[ConfigIssue]:
    p = settings.paths
    issues: list[ConfigIssue] = []

    def need_dir(name: str, path: Path) -> None:
        if not path.exists():
            issues.append(ConfigIssue("error", "missing_dir", name, f"not found: {path}"))
        elif not path.is_dir():
            issues.append(ConfigIssue("error", "not_a_dir", name, f"not a directory: {path}"))

    def need_file(name: str, path: Path) -> None:
        if not path.is_file():
            issues.append(ConfigIssue("error", "missing_file", name, f"not found: {path}"))

    try:
        cfg: Path | None = config_dir()
    except ConfigDirNotFound as exc:
        cfg = None
        issues.append(ConfigIssue("error", "no_config_dir", "config_dir", str(exc)))
    if cfg is not None:
        need_dir("config_dir", cfg)
        need_file("hardware_profile", cfg / "hardware" / f"{settings.hardware_profile}.toml")
    need_dir("workspace_root", p.workspace_root)
    for name, path in p.external_roots.items():
        need_dir(name, path)
    need_file("ltx_python", p.ltx_python)
    need_file("ltx.render_script", p.ltx_repo / settings.ltx.render_script)
    if not is_within(p.ltx_python, p.ltx_repo):
        issues.append(
            ConfigIssue(
                "warning",
                "ltx_python_outside_repo",
                "ltx_python",
                "python.exe is not inside ltx_repo; ADR-0003 expects the repository's own venv",
            )
        )
    try:
        need_dir("registry_dir", settings.effective_registry_dir)
    except ConfigDirNotFound:
        pass  # already reported as no_config_dir
    for name, path in p.studio_roots.items():
        if path.exists() and not path.is_dir():
            issues.append(ConfigIssue("error", "not_a_dir", name, f"not a directory: {path}"))
        elif not path.exists() and name in p.read_only_originals:
            # Read-only originals are never created by the studio.
            issues.append(
                ConfigIssue(
                    "info",
                    "originals_absent",
                    name,
                    "absent; this folder holds read-only originals and the studio never creates it",
                )
            )
        elif not path.exists():
            issues.append(
                ConfigIssue(
                    "info",
                    "will_be_created_later",
                    name,
                    "absent; created only by an authorized data-writing step (A4), never by config",
                )
            )
    # Lexical guards cannot see links; report any link on the way to a configured root.
    for name, path in p.all_paths.items():
        for link in find_reparse_points(path):
            issues.append(
                ConfigIssue(
                    "warning",
                    "reparse_point",
                    name,
                    f"{link} is a symlink/junction/reparse point; lexical path guards do not "
                    "follow it (see docs/security-model.md)",
                )
            )
    return issues
