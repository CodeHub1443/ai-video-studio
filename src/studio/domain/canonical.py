"""Canonical, hash-ready representation of plain data. Pure functions; no hashing, no I/O.

Rules (``CANONICAL_VERSION`` 1). Any change to them must bump the version, because it changes every
future fingerprint:

* Output is JSON, UTF-8, object keys sorted, no insignificant whitespace, no ASCII escaping.
* Allowed values: ``None``, ``bool``, ``int``, finite ``float``, ``str``, sequences (list/tuple) and
  mappings with ``str`` keys. Enums are replaced by their value. Sets, bytes, dates and objects are rejected.
* Type-preserving: ``1`` and ``1.0`` are different values and serialize differently.
* ``-0.0`` is normalized to ``0.0``; ``NaN`` and infinities are rejected.
* Integers must fit 64 bits (-2**63 .. 2**64-1) and are written as exact decimal text. Consumers outside
  Python must parse them as 64-bit integers, not doubles.
* Strings are used as given (no Unicode normalization): text is content, and silently rewriting it would
  hide edits. Strings that cannot be encoded as UTF-8 (lone surrogates) are rejected.
* Sequence order is significant and preserved. Mapping order is not.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from enum import Enum
from types import MappingProxyType
from typing import Any

from studio.domain.errors import fail
from studio.domain.ids import PARAM_KEY_RE

CANONICAL_VERSION = 1

_INT_MIN = -(2**63)
_INT_MAX = 2**64 - 1

type ParamValue = str | int | float | bool | None | tuple[ParamValue, ...] | Mapping[str, ParamValue]
type ParamMap = Mapping[str, ParamValue]


def _check_encodable(text: str, path: str) -> None:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        fail("canonical.bad_string", path, "string is not valid UTF-8 (lone surrogate?)")


def freeze_value(value: Any, path: str = "$") -> Any:
    """Return a deeply immutable copy: sequences become tuples, mappings become read-only sorted mappings."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, Enum):
        return freeze_value(value.value, path)
    if isinstance(value, str):
        _check_encodable(value, path)
        return value
    if isinstance(value, int):
        if not _INT_MIN <= value <= _INT_MAX:
            fail("canonical.int_range", path, "integer outside the 64-bit range")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            fail("canonical.non_finite", path, "NaN and infinity are not representable")
        return value + 0.0  # normalizes -0.0 to 0.0
    if isinstance(value, Mapping):
        items: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                fail("canonical.key_type", path, f"mapping key {key!r} is not a string")
            _check_encodable(key, f"{path}.{key}")
            items[key] = freeze_value(item, f"{path}.{key}")
        return MappingProxyType(dict(sorted(items.items())))
    if isinstance(value, (list, tuple)):
        return tuple(freeze_value(item, f"{path}[{i}]") for i, item in enumerate(value))
    fail("canonical.unsupported_type", path, f"{type(value).__name__} is not canonicalizable")


def thaw_value(value: Any) -> Any:
    """Inverse of ``freeze_value`` into plain ``dict``/``list`` (for serialization)."""
    if isinstance(value, Mapping):
        return {k: thaw_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw_value(v) for v in value]
    return value


def freeze_params(value: Any, path: str) -> ParamMap:
    """Freeze a parameter mapping and check its keys; ``None`` means empty."""
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        fail("params.type", path, "parameters must be a mapping")
    frozen = freeze_value(value, path)
    for key in frozen:
        if not PARAM_KEY_RE.fullmatch(key):
            fail("params.key", f"{path}.{key}", f"invalid parameter key {key!r}")
    return frozen


def canonical_json(value: Any) -> str:
    """Canonical JSON text of ``value`` (see module rules)."""
    return json.dumps(
        thaw_value(freeze_value(value)),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def canonical_bytes(value: Any) -> bytes:
    """Canonical UTF-8 bytes of ``value``: the input a later milestone feeds to a hash function."""
    return canonical_json(value).encode("utf-8")
