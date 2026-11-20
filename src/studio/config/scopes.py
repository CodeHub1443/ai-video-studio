"""Access policy: which ACTOR may perform which OPERATION where. Pure and LEXICAL (no stat, no resolve).

Ownership rules (approved decision 3):

* The studio writes, deletes and moves-from only inside studio-owned scopes.
* ComfyUI and LTX own writes to their own pinned output namespaces. The studio may READ (import) from
  them but must never write, delete or move-source-delete there.
* ``assets`` (originals), the shared models folder and the LTX weights are read-only to the studio.
* Protected LTX folders are closed to every actor for every operation.
* Everything not listed is denied (default deny).

``AccessPolicy.check`` is only a lexical PRE-CHECK. It cannot see symlinks, junctions or races, so it never
grants permission to touch the filesystem. The only grant is ``studio.config.guard.authorize``, which runs
this check and then real-path / reparse-point validation. See docs/security-model.md.

No file operation is implemented anywhere in this milestone.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from studio.config.namespaces import (
    COMFYUI_OUTPUT_SUBDIR,
    LTX_NAME_PREFIX,
    LTX_PREFIXED_FOLDERS,
    LTX_PROTECTED_FOLDERS,
    LTX_STUDIO_CATEGORY,
    LTX_VIDEOS_FOLDER,
)
from studio.config.paths import (
    PathValidationError,
    is_within,
    overlaps,
    parts_below,
    same_path,
    validate_absolute_windows_path,
)

if TYPE_CHECKING:
    from studio.config.settings import PathsConfig


class Actor(StrEnum):
    STUDIO = "studio"
    COMFYUI = "comfyui"
    LTX = "ltx"


class FsOp(StrEnum):
    READ = "read"
    WRITE = "write"  # create or modify (including creating directories inside a scope)
    DELETE = "delete"
    MOVE_SOURCE = "move_source"  # a move removes the source, so it is a delete there


_MUTATING = frozenset({FsOp.WRITE, FsOp.DELETE, FsOp.MOVE_SOURCE})
_ALL_OPS = frozenset(FsOp)


class AccessDenied(Exception):
    """An operation is not permitted by the access policy or failed real-path validation."""


class MatchMode(StrEnum):
    SUBTREE = "subtree"  # the base folder and everything below it
    NAME_PREFIX = (
        "name_prefix"  # only entries directly below base whose name starts with ``prefix``
    )


@dataclass(frozen=True)
class Scope:
    """A location and its owner. ``owner`` may mutate it; ``None`` means nobody (read-only)."""

    name: str
    base: Path
    mode: MatchMode
    owner: Actor | None
    prefix: str = ""
    description: str = ""

    def contains(self, path: Path | str) -> bool:
        if self.mode is MatchMode.SUBTREE:
            return same_path(path, self.base) or is_within(path, self.base)
        if not is_within(path, self.base):
            return False
        return parts_below(path, self.base)[0].startswith(self.prefix.casefold())

    def permits(self, actor: Actor, op: FsOp) -> bool:
        if actor is Actor.STUDIO:
            if self.owner is Actor.STUDIO:
                return True  # read, write, delete, move-source
            return op is FsOp.READ  # external, or read-only: the studio may only read/import
        # An engine process may only write inside the namespace it owns.
        return self.owner is actor and op in (FsOp.WRITE, FsOp.READ)

    def union_ops(self) -> frozenset[FsOp]:
        """Every operation some actor may perform here (used to detect overlap with deny rules)."""
        return frozenset(op for a in Actor for op in FsOp if self.permits(a, op))


@dataclass(frozen=True)
class DenyRule:
    name: str
    base: Path
    reason: str
    ops: frozenset[FsOp] = _ALL_OPS  # operations this rule forbids, for every actor

    def applies(self, path: Path | str, op: FsOp) -> bool:
        return op in self.ops and (same_path(path, self.base) or is_within(path, self.base))


@dataclass(frozen=True)
class AccessDecision:
    allowed: bool
    scope: Scope | None
    reason: str


@dataclass(frozen=True)
class AccessPolicy:
    scopes: tuple[Scope, ...]
    deny: tuple[DenyRule, ...]

    def check(self, path: Path | str, actor: Actor, op: FsOp) -> AccessDecision:
        """LEXICAL pre-check. A positive result is necessary, never sufficient (see ``guard.authorize``)."""
        try:
            validate_absolute_windows_path(path)
        except PathValidationError as exc:
            return AccessDecision(False, None, f"invalid path: {exc}")
        for rule in self.deny:
            if rule.applies(path, op):
                return AccessDecision(False, None, f"denied ({rule.name}): {rule.reason}")
        containing = [s for s in self.scopes if s.contains(path)]
        for scope in containing:
            if scope.permits(actor, op):
                return AccessDecision(True, scope, scope.description or "declared scope")
        if containing:
            s = containing[0]
            owner = s.owner.value if s.owner else "nobody (read-only)"
            return AccessDecision(
                False,
                None,
                f"{actor.value} may not {op.value} in scope {s.name!r} (owner: {owner})",
            )
        return AccessDecision(False, None, "outside every declared scope (default deny)")


def build_access_policy(paths: PathsConfig) -> AccessPolicy:
    """Derive the policy from configuration plus the pinned namespaces. Raises ``ValueError`` if a scope
    could be used for an operation that a deny rule forbids, or an engine scope leaves its engine tree."""
    S, C, L = Actor.STUDIO, Actor.COMFYUI, Actor.LTX
    sub, pre = MatchMode.SUBTREE, MatchMode.NAME_PREFIX
    ltx = paths.ltx_repo
    scopes: list[Scope] = [
        # studio-owned
        Scope("projects", paths.projects_root, sub, S),
        Scope(
            "studio_outputs",
            paths.studio_outputs_dir,
            sub,
            S,
            description="the only writable part of outputs_root",
        ),
        Scope("cache", paths.cache_root, sub, S),
        Scope("logs", paths.logs_root, sub, S),
        Scope("data", paths.data_root, sub, S),
        # read-only to the studio, owned by nobody
        Scope("assets", paths.assets_root, sub, None, description="read-only originals"),
        Scope("models", paths.models_root, sub, None, description="shared models, read-only"),
        Scope("ltx_models", paths.ltx_models, sub, None, description="LTX weights, read-only"),
        # engine-owned namespaces: the engine writes, the studio only reads/imports
        Scope(
            "comfyui_studio_output",
            paths.comfyui_output_dir / COMFYUI_OUTPUT_SUBDIR,
            sub,
            C,
            description="ComfyUI output namespace (ADR-0002)",
        ),
        Scope(
            "ltx_videos",
            ltx / LTX_VIDEOS_FOLDER / LTX_STUDIO_CATEGORY,
            sub,
            L,
            description="LTX videos/studio (ADR-0003)",
        ),
        *(
            Scope(
                f"ltx_{folder}",
                ltx / folder,
                pre,
                L,
                prefix=LTX_NAME_PREFIX,
                description=f"LTX {folder}/{LTX_NAME_PREFIX}* (ADR-0003)",
            )
            for folder in LTX_PREFIXED_FOLDERS
        ),
    ]
    deny = [
        DenyRule("assets", paths.assets_root, "assets are read-only originals", _MUTATING),
        DenyRule("software", paths.software_root, "the source tree is never mutated", _MUTATING),
        DenyRule("models", paths.models_root, "shared models are read-only", _MUTATING),
        DenyRule("ltx_models", paths.ltx_models, "LTX weights are read-only", _MUTATING),
        *(
            DenyRule(
                f"ltx_{name}",
                ltx / name,
                "protected LTX files (ADR-0003; import is undecided, D11)",
            )
            for name in LTX_PROTECTED_FOLDERS
        ),
    ]
    for scope in scopes:
        for rule in deny:
            if overlaps(scope.base, rule.base) and scope.union_ops() & rule.ops:
                raise ValueError(
                    f"scope {scope.name!r} could perform operations denied by {rule.name!r}"
                )
        if scope.owner in (C, L):
            engine_root = paths.comfyui_root if scope.owner is C else paths.ltx_repo
            if not is_within(scope.base, engine_root):
                raise ValueError(f"scope {scope.name!r} must be inside {engine_root}")
    return AccessPolicy(scopes=tuple(scopes), deny=tuple(deny))
