"""Mandatory real-path validation: the ONLY way to obtain permission to touch a path.

``authorize`` is the single entry point that any future file operation (milestones that write, import,
move or delete) must pass through. It performs NO file operation itself; it only inspects (``lexists``,
``lstat``, ``realpath``) and returns an immutable ``Authorization`` that records what was verified.

What it enforces, for the STUDIO actor and any operation:
1. the lexical policy pre-check (``AccessPolicy.check``) allows it;
2. the deepest existing ancestor-or-self of the target has a real path identical to its lexical path
   (so no symlink, junction, ``subst``/mapped-drive alias or 8.3 alias lies anywhere above it);
3. no existing component from the scope base down is a reparse point of any kind;
4. an existing target's real path is still inside the same scope;
5. a mutating operation on an existing file with more than one hard link is refused, because a second name
   may lie outside the scope (proposal, awaiting approval; see ADR-0009).
Any failure raises ``AccessDenied`` (fail closed).

TOCTOU: everything above is a CHECK, and the filesystem can change after it. ``Authorization`` is therefore
a pre-condition, not a lock. The file layer that consumes it (not written yet) MUST follow the protocol in
docs/security-model.md section 4: call ``revalidate`` immediately before use, open handles WITHOUT
following reparse points, verify the final path of the opened handle against the scope, and operate through
the handle, never through the path string again.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Protocol

from studio.config.checks import is_reparse_point
from studio.config.paths import is_within, same_path
from studio.config.scopes import AccessDenied, AccessPolicy, Actor, FsOp

_SEAL = object()


class FsProbe(Protocol):
    """Read-only filesystem inspection, injectable so the guard can be tested without a real disk."""

    def lexists(self, path: str) -> bool: ...
    def realpath(self, path: str) -> str: ...
    def is_reparse_point(self, path: str) -> bool: ...
    def identity(self, path: str) -> tuple[int, int] | None: ...
    def link_count(self, path: str) -> int: ...


class OsProbe:
    """Default probe: ``os.path.lexists``, ``os.path.realpath``, ``lstat`` and ``stat``. Writes nothing."""

    def lexists(self, path: str) -> bool:
        return os.path.lexists(path)

    def realpath(self, path: str) -> str:
        return os.path.realpath(path)

    def is_reparse_point(self, path: str) -> bool:
        return is_reparse_point(Path(path))

    def identity(self, path: str) -> tuple[int, int] | None:
        try:
            st = os.stat(path)
        except OSError:
            return None
        return (st.st_dev, st.st_ino)

    def link_count(self, path: str) -> int:
        try:
            return os.stat(path).st_nlink
        except OSError:
            return 1


@dataclass(frozen=True)
class Authorization:
    """Proof that ``authorize`` verified ``path`` for ``op`` at one moment. Not constructible directly."""

    path: Path
    scope_name: str
    op: FsOp
    anchor: Path  # deepest existing ancestor-or-self at verification time
    anchor_identity: tuple[int, int] | None
    _seal: object = field(repr=False, compare=False, default=None)

    def __post_init__(self) -> None:
        if self._seal is not _SEAL:
            raise TypeError("Authorization can only be issued by studio.config.guard.authorize")


def _deepest_existing(path: Path, probe: FsProbe) -> Path:
    pw = PureWindowsPath(str(path))
    for i in range(len(pw.parts), 0, -1):
        candidate = Path(*pw.parts[:i])
        if probe.lexists(str(candidate)):
            return candidate
    raise AccessDenied(f"{path}: not even the drive root exists")


def authorize(
    policy: AccessPolicy,
    path: Path | str,
    op: FsOp,
    *,
    actor: Actor = Actor.STUDIO,
    probe: FsProbe | None = None,
) -> Authorization:
    """Return an ``Authorization`` or raise ``AccessDenied``. Inspects only; changes nothing."""
    if actor is not Actor.STUDIO:
        raise AccessDenied(
            "authorize is for studio operations; engine output targets are validated lexically with "
            "AccessPolicy.check before being handed to the engine"
        )
    probe = probe or OsProbe()
    target = Path(path)
    decision = policy.check(target, actor, op)
    if not decision.allowed or decision.scope is None:
        raise AccessDenied(f"{target}: {decision.reason}")
    scope = decision.scope

    anchor = _deepest_existing(target, probe)
    real_anchor = probe.realpath(str(anchor))
    if not same_path(real_anchor, anchor):
        raise AccessDenied(
            f"{target}: {anchor} resolves to {real_anchor}; a link or alias lies on the path"
        )
    # No reparse point of any kind from the scope base down to the anchor.
    chain = PureWindowsPath(str(anchor)).parts
    for i in range(1, len(chain) + 1):
        comp = Path(*chain[:i])
        in_scope = same_path(comp, scope.base) or is_within(comp, scope.base)
        if (in_scope or same_path(comp, anchor)) and probe.is_reparse_point(str(comp)):
            raise AccessDenied(f"{target}: {comp} is a symlink/junction/reparse point")
    if same_path(anchor, target):  # the target itself exists
        real = probe.realpath(str(target))
        if not same_path(real, target):
            raise AccessDenied(f"{target}: resolves to {real}")
        if not scope.contains(real):
            raise AccessDenied(f"{target}: real path {real} left scope {scope.name!r}")
        if op is not FsOp.READ and probe.link_count(str(target)) > 1:
            raise AccessDenied(f"{target}: has multiple hard links; refusing to {op.value}")
    return Authorization(
        path=target,
        scope_name=scope.name,
        op=op,
        anchor=anchor,
        anchor_identity=probe.identity(str(anchor)),
        _seal=_SEAL,
    )


def revalidate(
    policy: AccessPolicy, auth: Authorization, *, probe: FsProbe | None = None
) -> Authorization:
    """Re-run ``authorize`` and require the same scope and the same anchor identity (detects swaps).

    Call immediately before use. It narrows, but cannot close, the TOCTOU window (see module docstring).
    """
    fresh = authorize(policy, auth.path, auth.op, probe=probe)
    if fresh.scope_name != auth.scope_name:
        raise AccessDenied(f"{auth.path}: scope changed from {auth.scope_name!r} to {fresh.scope_name!r}")
    if same_path(fresh.anchor, auth.anchor) and fresh.anchor_identity != auth.anchor_identity:
        raise AccessDenied(f"{auth.path}: {auth.anchor} was replaced since authorization")
    return fresh
