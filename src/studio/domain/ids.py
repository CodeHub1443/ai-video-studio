"""Identifier types and lexical checks. Ids are opaque strings; this module never generates them
(generation, e.g. ULIDs, belongs to persistence)."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import NewType

from studio.domain.errors import fail

ProjectId = NewType("ProjectId", str)
SceneId = NewType("SceneId", str)
ShotId = NewType("ShotId", str)
TakeId = NewType("TakeId", str)
AssetId = NewType("AssetId", str)
VersionId = NewType("VersionId", str)
JobId = NewType("JobId", str)
AttemptId = NewType("AttemptId", str)
GateId = NewType("GateId", str)
ApprovalId = NewType("ApprovalId", str)

# Entity ids: ASCII, start alphanumeric, no separators that could be mistaken for paths.
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
# Project slug (names the future projects/<slug> folder): lowercase words joined by '-'.
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
# Registry ids: same lexical rule as studio.registry.schema.RegistryId (kept as a copy on purpose: the
# domain must not import the registry, which drags in the pydantic-based configuration).
REGISTRY_ID_RE = re.compile(r"^[a-z][a-z0-9_]*(?:[.:][a-z0-9_]+)*$")
# Parameter keys and slot roles.
PARAM_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
ROLE_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_SLUG_LEN = 48
MAX_REGISTRY_ID_LEN = 96


def check_id(value: object, path: str, pattern: re.Pattern[str] = ID_RE) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        fail("id.invalid", path, f"{value!r} is not a valid identifier (expected {pattern.pattern})")
    return value


def check_optional_id(value: object, path: str, pattern: re.Pattern[str] = ID_RE) -> str | None:
    return None if value is None else check_id(value, path, pattern)


def check_slug(value: object, path: str) -> str:
    check_id(value, path, SLUG_RE)
    assert isinstance(value, str)
    if len(value) > MAX_SLUG_LEN:
        fail("slug.too_long", path, f"slug longer than {MAX_SLUG_LEN} characters")
    return value


def check_registry_id(value: object, path: str) -> str:
    check_id(value, path, REGISTRY_ID_RE)
    assert isinstance(value, str)
    if len(value) > MAX_REGISTRY_ID_LEN:
        fail("registry_id.too_long", path, f"registry id longer than {MAX_REGISTRY_ID_LEN} characters")
    return value


def check_sha256(value: object, path: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        fail("sha256.invalid", path, "expected 64 lowercase hexadecimal characters")
    return value


def check_text(value: object, path: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        fail("text.type", path, "expected a string")
    if not allow_empty and not value.strip():
        fail("text.empty", path, "must not be empty")
    return value


def check_unique_ids(values: Iterable[object], path: str) -> tuple[str, ...]:
    checked = tuple(check_id(v, f"{path}[{i}]") for i, v in enumerate(values))
    if len(set(checked)) != len(checked):
        fail("id.duplicate", path, "contains duplicate ids")
    return checked
