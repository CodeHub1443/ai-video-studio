"""Error and issue types of the domain layer. Pure data; no I/O."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import NoReturn


@dataclass(frozen=True)
class ValidationIssue:
    """One validation finding. ``code`` is a stable machine-readable id, ``path`` locates the subject
    (e.g. ``shots[shot_1].selected_take_id``), ``message`` is for humans."""

    code: str
    path: str
    message: str

    def __str__(self) -> str:
        return f"[{self.code}] {self.path}: {self.message}"


class DomainError(Exception):
    """Base class of all domain-layer errors."""


class DomainValidationError(DomainError, ValueError):
    """An entity or snapshot violates a structural or referential rule."""

    def __init__(self, issues: Iterable[ValidationIssue]) -> None:
        self.issues: tuple[ValidationIssue, ...] = tuple(issues)
        super().__init__("; ".join(str(i) for i in self.issues) or "invalid domain object")


class InvalidTransition(DomainError):
    """A state change that the transition table does not allow."""

    def __init__(self, table: str, source: str, target: str) -> None:
        self.table = table
        self.source = source
        self.target = target
        super().__init__(f"{table}: transition {source} -> {target} is not allowed")


class UnresolvedSpecError(DomainError):
    """A fingerprint was requested for a spec that is not fully resolved (seed, pinned inputs, binding)."""


def fail(code: str, path: str, message: str) -> NoReturn:
    raise DomainValidationError((ValidationIssue(code, path, message),))


def raise_if_issues(issues: Iterable[ValidationIssue]) -> None:
    collected = tuple(issues)
    if collected:
        raise DomainValidationError(collected)
