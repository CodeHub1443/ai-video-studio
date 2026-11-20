"""Pure, LEXICAL path helpers for Windows paths. They never touch the filesystem (no stat, no mkdir,
no resolve), so they cannot see symlinks, junctions or other reparse points. See docs/security-model.md.

All comparisons use ``ntpath`` explicitly so behaviour does not depend on the host OS.
"""

from __future__ import annotations

import ntpath
import re
from pathlib import Path, PureWindowsPath

_SEP = "\\"

# ':' also rejects drive components ("C:x") and NTFS alternate data streams ("a.txt:s", "a::$DATA").
# '/' and '\' are rejected inside a single segment.
_INVALID_CHARS = frozenset('<>:"|?*/\\') | frozenset(chr(i) for i in range(32))
_RESERVED_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul", "conin$", "conout$", "clock$"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
    | {f"{p}{s}" for p in ("com", "lpt") for s in ("¹", "²", "³")}
)
# 8.3 short-name aliases ("PROGRA~1"): two spellings of one directory defeat lexical comparison.
_SHORT_NAME = re.compile(r"~\d")
_DRIVE = re.compile(r"^[A-Za-z]:$")

MAX_SEGMENT_LEN = 255
MAX_RELATIVE_LEN = 240


class PathValidationError(ValueError):
    """A path or path segment failed lexical validation."""


def validate_segment(segment: str) -> str:
    """Validate ONE path component (a file or folder name). Returns it unchanged or raises."""
    if not segment:
        raise PathValidationError("empty path segment")
    if segment in (".", ".."):
        raise PathValidationError(f"segment {segment!r} is not allowed")
    if len(segment) > MAX_SEGMENT_LEN:
        raise PathValidationError(f"segment longer than {MAX_SEGMENT_LEN} characters")
    bad = sorted({c for c in segment if c in _INVALID_CHARS})
    if bad:
        raise PathValidationError(
            f"segment {segment!r} contains forbidden characters {bad!r} "
            "(':' also rules out drive components and NTFS alternate data streams)"
        )
    if segment[-1] in ". ":
        raise PathValidationError(f"segment {segment!r} ends with a dot or space (Windows strips it)")
    if segment.split(".", 1)[0].rstrip(" ").casefold() in _RESERVED_DEVICE_NAMES:
        raise PathValidationError(f"segment {segment!r} is a reserved Windows device name")
    if _SHORT_NAME.search(segment):
        raise PathValidationError(f"segment {segment!r} looks like an 8.3 short name; use the long name")
    return segment


def validate_relative_posix(path: str, *, allow_dot: bool = False) -> str:
    """Validate a relative, forward-slash path (as used in registry files). Returns it or raises.

    ``"."`` (the root itself) is accepted only when ``allow_dot`` is true.
    """
    if path == ".":
        if allow_dot:
            return path
        raise PathValidationError("'.' is not allowed here")
    if not path:
        raise PathValidationError("empty path")
    if "\\" in path:
        raise PathValidationError("use forward slashes")
    if path.startswith("/"):
        raise PathValidationError("path must be relative")
    if len(path) > MAX_RELATIVE_LEN:
        raise PathValidationError(f"path longer than {MAX_RELATIVE_LEN} characters")
    for segment in path.split("/"):
        validate_segment(segment)
    return path


def validate_absolute_windows_path(path: Path | str) -> None:
    """Lexically validate an absolute path on a lettered drive. Raises ``PathValidationError``.

    Rejects UNC paths, ``\\\\?\\`` / ``\\\\.\\`` device paths, drive-relative paths (``C:foo``),
    and any component rejected by ``validate_segment`` (including ``..``).
    """
    pw = PureWindowsPath(str(path))
    if not _DRIVE.match(pw.drive) or not pw.root:
        raise PathValidationError(
            f"{str(path)!r} must be absolute on a lettered drive (UNC, device and drive-relative "
            "paths are not allowed)"
        )
    for segment in pw.parts[1:]:
        validate_segment(segment)


def _norm(p: Path | str) -> str:
    return ntpath.normcase(ntpath.normpath(str(p)))


def same_path(a: Path | str, b: Path | str) -> bool:
    return _norm(a) == _norm(b)


def is_within(child: Path | str, parent: Path | str) -> bool:
    """True if ``child`` is strictly below ``parent`` (case-insensitive, lexical only)."""
    c, p = _norm(child), _norm(parent)
    if c == p:
        return False
    return c.startswith(p.rstrip(_SEP) + _SEP)


def overlaps(a: Path | str, b: Path | str) -> bool:
    """True if the two paths are equal or one contains the other."""
    return same_path(a, b) or is_within(a, b) or is_within(b, a)


def parts_below(child: Path | str, parent: Path | str) -> tuple[str, ...]:
    """Case-folded components of ``child`` below ``parent``; empty if equal; ``ValueError`` if not within."""
    if same_path(child, parent):
        return ()
    if not is_within(child, parent):
        raise ValueError(f"{child} is not inside {parent}")
    c, p = _norm(child), _norm(parent)
    return tuple(c[len(p.rstrip(_SEP)) + 1 :].split(_SEP))
