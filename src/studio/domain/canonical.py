"""Canonical, hash-ready representation of plain data. Pure functions; no hashing, no I/O.

Rules (``CANONICAL_VERSION`` 2). Any change to them must bump the version, because it changes every
future fingerprint. The output is byte-identical to RFC 8785 (JSON Canonicalization Scheme, JCS) for
every value this module admits, so an independent tool can reproduce it:

* Output is JSON, UTF-8, object keys sorted by code point, no insignificant whitespace, no ASCII escaping.
* Allowed values: ``None``, ``bool``, ``int``, finite ``float``, ``str``, sequences (list/tuple) and
  mappings with ASCII ``str`` keys. Enums are replaced by their value. Sets, bytes, dates and objects are
  rejected.
* Numbers follow the JCS number model: one numeric value has one text. ``24`` and ``24.0`` both serialize as
  ``24``. Floats are written like ECMAScript ``Number.prototype.toString`` (``format_number``).
* Integers must lie within +-(2**53 - 1), the range a double represents exactly. Larger values (seeds,
  sizes) are typed fields that their owners encode as decimal strings. The range applies to integers only:
  floats follow RFC 8785 as they are. There is no byte collision between the two, because an integer beyond
  the range is rejected, and an integral float prints as the same text as the equal integer (one value, one
  text). Integer text comes from the true value (``int.__repr__``), so an ``int`` subclass can never change the
  bytes.
* ``-0.0`` serializes as ``0``; ``NaN`` and infinities are rejected.
* Keys are ASCII only, so code-point order equals the UTF-16 order JCS prescribes.
* Strings are used as given (no Unicode normalization): text is content, and silently rewriting it would
  hide edits. Strings that cannot be encoded as UTF-8 (lone surrogates) are rejected. Escaping is the JCS
  escaping (``"``, ``\\``, and control characters only).
* Sequence order is significant and preserved. Mapping order is not. Collections whose order carries no
  meaning (for example a spec's references) are sorted by their owners before they get here.
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

CANONICAL_VERSION = 2

# Largest integer magnitude a JSON number may carry (exact in an IEEE-754 double).
SAFE_INT_MAX = 2**53 - 1

type ParamValue = (
    str | int | float | bool | None | tuple[ParamValue, ...] | Mapping[str, ParamValue]
)
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
        if not -SAFE_INT_MAX <= value <= SAFE_INT_MAX:
            fail("canonical.int_range", path, "integer outside +-(2**53 - 1)")
        return int.__index__(value)  # a plain int, even for an int subclass
    if isinstance(value, float):
        if not math.isfinite(value):
            fail("canonical.non_finite", path, "NaN and infinity are not representable")
        return value + 0.0  # normalizes -0.0 to 0.0
    if isinstance(value, Mapping):
        items: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                fail("canonical.key_type", path, f"mapping key {key!r} is not a string")
            if not key.isascii():
                fail("canonical.key_ascii", f"{path}.{key}", "mapping keys must be ASCII")
            items[key] = freeze_value(item, f"{path}.{key}")
        return MappingProxyType(dict(sorted(items.items())))
    if isinstance(value, (list, tuple)):
        return tuple(freeze_value(item, f"{path}[{i}]") for i, item in enumerate(value))
    fail("canonical.unsupported_type", path, f"{type(value).__name__} is not canonicalizable")


def thaw_value(value: Any) -> Any:
    """Inverse of ``freeze_value`` into plain ``dict``/``list``."""
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


def format_number(value: float) -> str:
    """ECMAScript ``Number::toString`` of a finite double (the JCS number serialization).

    The shortest digit string that round-trips (``s`` with ``k`` digits) comes from Python's ``repr``, which is
    correctly rounded and shortest. With ``n`` such that the value is ``0.s * 10**n``, the layout is:
    ``k <= n <= 21``: digits then ``n - k`` zeros; ``0 < n <= 21``: a decimal point after ``n`` digits;
    ``-6 < n <= 0``: ``0.`` then ``-n`` zeros then the digits; otherwise exponent form ``d[.ddd]e+/-x`` with
    ``x = n - 1``.
    """
    if not math.isfinite(value):
        fail("canonical.non_finite", "$", "NaN and infinity are not representable")
    if value == 0:
        return "0"
    if value < 0:
        return "-" + format_number(-value)
    mantissa, _, exponent = repr(value).partition("e")
    whole, _, fraction = mantissa.partition(".")
    digits = whole + fraction
    point = len(whole) + (int(exponent) if exponent else 0)
    stripped = digits.lstrip("0")
    point -= len(digits) - len(stripped)
    digits = stripped.rstrip("0")
    k, n = len(digits), point
    if k <= n <= 21:
        return digits + "0" * (n - k)
    if 0 < n <= 21:
        return f"{digits[:n]}.{digits[n:]}"
    if -6 < n <= 0:
        return "0." + "0" * -n + digits
    sign = "+" if n - 1 >= 0 else "-"
    head = digits[0] if k == 1 else f"{digits[0]}.{digits[1:]}"
    return f"{head}e{sign}{abs(n - 1)}"


def _emit(value: Any, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, int):
        out.append(int.__repr__(value))  # never the subclass's own __str__/__repr__
    elif isinstance(value, float):
        out.append(format_number(value))
    elif isinstance(value, str):
        out.append(json.dumps(value, ensure_ascii=False))
    elif isinstance(value, Mapping):
        out.append("{")
        for i, (key, item) in enumerate(value.items()):
            if i:
                out.append(",")
            out.append(json.dumps(key, ensure_ascii=False))
            out.append(":")
            _emit(item, out)
        out.append("}")
    else:  # a tuple: freeze_value admits nothing else
        out.append("[")
        for i, item in enumerate(value):
            if i:
                out.append(",")
            _emit(item, out)
        out.append("]")


def canonical_json(value: Any) -> str:
    """Canonical JSON text of ``value`` (see module rules)."""
    out: list[str] = []
    _emit(freeze_value(value), out)
    return "".join(out)


def canonical_bytes(value: Any) -> bytes:
    """Canonical UTF-8 bytes of ``value``: the input of every fingerprint digest."""
    return canonical_json(value).encode("utf-8")
