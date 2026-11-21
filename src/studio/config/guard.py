"""Mandatory real-path validation: the ONLY way to obtain permission to touch a path.

``authorize`` is the single entry point that any future file operation (milestones that write, import,
move or delete) must pass through. It performs NO file operation itself; it only inspects (``lexists``,
``lstat``, ``realpath``) and returns an immutable, sealed ``Authorization`` that records what was verified.

What it enforces, for the STUDIO actor and any operation:
1. the lexical policy pre-check (``AccessPolicy.check``) allows it;
2. the deepest existing ancestor-or-self of the target has a real path identical to its lexical path
   (so no symlink, junction, ``subst``/mapped-drive alias or 8.3 alias lies anywhere above it);
3. no existing component from the scope base down is a reparse point of any kind;
4. an existing target's real path is still inside the same scope;
5. an existing target with more than one hard link (a second name may lie outside the scope): a mutating
   operation is refused; a READ is refused in studio-owned and engine scopes and allowed in read-only scopes
   (models, LTX weights, assets, where users keep deduplicated hard links), recorded as
   ``Authorization.multi_link`` (ADR-0008 P-2, user decision I-13);
6. fail closed: when the probe cannot answer (existence, reparse state, link count or identity unknown)
   the operation is denied.
Any failure raises ``AccessDenied``.

``authorize_child`` authorizes one direct child of an already authorized directory. It first revalidates
the parent (same identity), then re-runs every check for the child. There is deliberately no
recursive-delete API: a caller walks with handles and authorizes each entry itself.

What ``Authorization`` guarantees: it can only be built with a private token held by this module, it
refuses attribute assignment, subclassing, copying and pickling, and ``is_issued`` is True only for an
object that ``authorize`` / ``authorize_child`` produced and whose fields still equal the values recorded
at issue time (so a rewrite through ``object.__setattr__`` or a slot descriptor is detected). What it does
NOT guarantee: protection against hostile code in the same process that reaches the private token or the
private registry; that is outside the threat model. Consumers (``revalidate``, ``authorize_child`` and
the future file layer) must call ``is_issued``.

TOCTOU: everything above is a CHECK, and the filesystem can change after it. ``Authorization`` is therefore
a pre-condition, not a lock. The file layer that consumes it (not written yet) MUST follow the protocol in
docs/security-model.md section 4: call ``revalidate`` immediately before use, open handles WITHOUT
following reparse points, verify the final path of the opened handle against the scope, and operate through
the handle, never through the path string again.
"""

from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath
from typing import Any, NoReturn, Protocol
from weakref import ref

from studio.config.checks import probe_reparse_point
from studio.config.paths import PathValidationError, is_within, same_path, validate_segment
from studio.config.scopes import AccessDenied, AccessPolicy, Actor, FsOp

_ISSUE_TOKEN = object()
_FIELDS = ("path", "scope_name", "op", "anchor", "anchor_identity", "multi_link")
# id(auth) -> (weak reference, values recorded at issue time). The reference's callback drops the entry
# when the object is collected, so a reused id can never inherit it.
_ISSUED: dict[int, tuple[ref[Authorization], tuple[Any, ...]]] = {}
_PARENT_OPS = frozenset({FsOp.WRITE, FsOp.DELETE})
_CHILD_OPS = frozenset({FsOp.WRITE, FsOp.DELETE, FsOp.MOVE_SOURCE})


class FsProbe(Protocol):
    """Read-only filesystem inspection, injectable so the guard can be tested without a real disk.

    ``lexists``, ``is_reparse_point``, ``identity`` and ``link_count`` return ``None`` when the probe could
    not determine the answer (an error other than plain absence); the guard treats that as a denial,
    never as a default.
    """

    def lexists(self, path: str) -> bool | None: ...
    def realpath(self, path: str) -> str: ...
    def is_reparse_point(self, path: str) -> bool | None: ...
    def identity(self, path: str) -> tuple[int, int] | None: ...
    def link_count(self, path: str) -> int | None: ...


class OsProbe:
    """Default probe: ``lstat``, ``os.path.realpath`` and ``stat``. Writes nothing."""

    def lexists(self, path: str) -> bool | None:
        """False only when the path is absent; any other failure is unknown (``None``)."""
        try:
            os.lstat(path)
        except (FileNotFoundError, NotADirectoryError):
            return False
        except (OSError, ValueError):
            return None
        return True

    def realpath(self, path: str) -> str:
        return os.path.realpath(path)

    def is_reparse_point(self, path: str) -> bool | None:
        return probe_reparse_point(Path(path))

    def identity(self, path: str) -> tuple[int, int] | None:
        try:
            st = os.stat(path)
        except OSError:
            return None
        return (st.st_dev, st.st_ino)

    def link_count(self, path: str) -> int | None:
        try:
            return os.stat(path).st_nlink
        except OSError:
            return None


class Authorization:
    """Proof that ``authorize`` verified ``path`` for ``op`` at one moment.

    Not constructible by callers, read-only, final, and neither copyable nor picklable. Use ``is_issued``
    to tell a real one from a forgery.
    """

    __slots__ = (
        "__weakref__",
        "anchor",
        "anchor_identity",
        "multi_link",
        "op",
        "path",
        "scope_name",
    )

    path: Path
    scope_name: str
    op: FsOp
    anchor: Path  # deepest existing ancestor-or-self at verification time
    anchor_identity: tuple[int, int] | None
    multi_link: (
        bool  # the target is a multi-link file that was allowed because the scope is read-only
    )

    def __init__(
        self,
        token: object,
        *,
        path: Path,
        scope_name: str,
        op: FsOp,
        anchor: Path,
        anchor_identity: tuple[int, int] | None,
        multi_link: bool = False,
    ) -> None:
        if token is not _ISSUE_TOKEN:
            raise TypeError("Authorization can only be issued by studio.config.guard.authorize")
        values: dict[str, Any] = {
            "path": path,
            "scope_name": scope_name,
            "op": op,
            "anchor": anchor,
            "anchor_identity": anchor_identity,
            "multi_link": multi_link,
        }
        for name, value in values.items():
            object.__setattr__(self, name, value)
        _register(self)

    def __init_subclass__(cls, **kwargs: Any) -> None:
        raise TypeError("Authorization cannot be subclassed")

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Authorization is read-only")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("Authorization is read-only")

    def __copy__(self) -> NoReturn:
        raise TypeError("Authorization cannot be copied")

    def __deepcopy__(self, memo: object) -> NoReturn:
        raise TypeError("Authorization cannot be copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("Authorization cannot be pickled")

    def __reduce_ex__(self, protocol: object) -> NoReturn:
        raise TypeError("Authorization cannot be pickled")

    def __repr__(self) -> str:
        return (
            f"Authorization(path={self.path!r}, scope={self.scope_name!r}, op={self.op.value!r}, "
            f"multi_link={self.multi_link})"
        )


def _values(auth: Authorization) -> tuple[Any, ...]:
    return tuple(getattr(auth, name) for name in _FIELDS)


def _register(auth: Authorization) -> None:
    key = id(auth)

    def drop(dead: ref[Authorization]) -> None:
        entry = _ISSUED.get(key)
        if entry is not None and entry[0] is dead:
            del _ISSUED[key]

    _ISSUED[key] = (ref(auth, drop), _values(auth))


def is_issued(auth: object) -> bool:
    """True only for an object that ``authorize`` / ``authorize_child`` produced in this process AND whose
    fields still equal the values recorded when it was issued (same type and equal, field by field)."""
    if not isinstance(auth, Authorization):
        return False
    entry = _ISSUED.get(id(auth))
    if entry is None or entry[0]() is not auth:
        return False
    try:
        current = _values(auth)
    except AttributeError:  # a slot was never set or was deleted
        return False
    return all(type(a) is type(b) and a == b for a, b in zip(entry[1], current, strict=True))


def _issue(
    path: Path,
    scope_name: str,
    op: FsOp,
    anchor: Path,
    anchor_identity: tuple[int, int] | None,
    multi_link: bool,
) -> Authorization:
    return Authorization(
        _ISSUE_TOKEN,
        path=path,
        scope_name=scope_name,
        op=op,
        anchor=anchor,
        anchor_identity=anchor_identity,
        multi_link=multi_link,
    )


def _deepest_existing(path: Path, probe: FsProbe) -> Path:
    pw = PureWindowsPath(str(path))
    for i in range(len(pw.parts), 0, -1):
        candidate = Path(*pw.parts[:i])
        exists = probe.lexists(str(candidate))
        if exists is None:
            raise AccessDenied(f"{path}: existence of {candidate} unknown; refusing")
        if exists:
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
        if in_scope or same_path(comp, anchor):
            link = probe.is_reparse_point(str(comp))
            if link is None:
                raise AccessDenied(f"{target}: reparse state of {comp} unknown; refusing")
            if link:
                raise AccessDenied(f"{target}: {comp} is a symlink/junction/reparse point")
    multi_link = False
    if same_path(anchor, target):  # the target itself exists
        real = probe.realpath(str(target))
        if not same_path(real, target):
            raise AccessDenied(f"{target}: resolves to {real}")
        if not scope.contains(real):
            raise AccessDenied(f"{target}: real path {real} left scope {scope.name!r}")
        links = probe.link_count(str(target))
        if links is None:
            raise AccessDenied(f"{target}: hard-link count unknown; refusing to {op.value}")
        if links > 1:
            if op is FsOp.READ and scope.owner is None:
                multi_link = True  # read-only scope: allowed, and recorded
            else:
                raise AccessDenied(
                    f"{target}: has multiple hard links; refusing to {op.value} in scope "
                    f"{scope.name!r}"
                )
    identity = probe.identity(str(anchor))
    if identity is None:
        raise AccessDenied(f"{target}: identity of {anchor} unknown; refusing to {op.value}")
    return _issue(target, scope.name, op, anchor, identity, multi_link)


def authorize_child(
    policy: AccessPolicy,
    parent: Authorization,
    name: str,
    op: FsOp,
    *,
    probe: FsProbe | None = None,
) -> Authorization:
    """Authorize one DIRECT child of an authorized directory, re-running every check for the child.

    ``parent`` must be a genuine ``Authorization`` for WRITE or DELETE and is revalidated first (same
    identity, same scope). ``name`` is a single path segment. The child is never inferred from the parent:
    it goes through the whole of ``authorize``, so a link, an alias, a scope change or a multi-link file
    below the parent is refused.
    """
    if not is_issued(parent):
        raise AccessDenied("parent is not an Authorization issued by authorize")
    if parent.op not in _PARENT_OPS:
        raise AccessDenied(
            f"{parent.path}: parent was authorized for {parent.op.value}, not a mutation"
        )
    if op not in _CHILD_OPS:
        raise AccessDenied(f"child operation {op.value!r} is not a mutation")
    try:
        validate_segment(name)
    except PathValidationError as exc:
        raise AccessDenied(f"invalid child name {name!r}: {exc}") from None
    parent = revalidate(policy, parent, probe=probe)
    child = authorize(policy, parent.path / name, op, probe=probe)
    if child.scope_name != parent.scope_name:
        raise AccessDenied(f"{child.path}: scope {child.scope_name!r} differs from the parent's")
    if child.multi_link:
        raise AccessDenied(f"{child.path}: multi-link children are refused")
    return child


def revalidate(
    policy: AccessPolicy, auth: Authorization, *, probe: FsProbe | None = None
) -> Authorization:
    """Re-run ``authorize`` and require the same scope and the same anchor identity (detects swaps).

    Refuses anything that is not a genuine ``Authorization``. Call immediately before use. It narrows, but
    cannot close, the TOCTOU window (see module docstring).
    """
    if not is_issued(auth):
        raise AccessDenied("not an Authorization issued by authorize")
    if auth.anchor_identity is None:
        raise AccessDenied(f"{auth.path}: authorization has no anchor identity")
    fresh = authorize(policy, auth.path, auth.op, probe=probe)
    if fresh.anchor_identity is None:
        raise AccessDenied(f"{auth.path}: anchor identity unknown")
    if fresh.scope_name != auth.scope_name:
        raise AccessDenied(
            f"{auth.path}: scope changed from {auth.scope_name!r} to {fresh.scope_name!r}"
        )
    if same_path(fresh.anchor, auth.anchor) and fresh.anchor_identity != auth.anchor_identity:
        raise AccessDenied(f"{auth.path}: {auth.anchor} was replaced since authorization")
    if fresh.multi_link and not auth.multi_link:
        raise AccessDenied(f"{auth.path}: gained additional hard links since authorization")
    return fresh
