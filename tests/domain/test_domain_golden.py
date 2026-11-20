"""Golden vectors (design B.6): hand-written canonical bytes and digests, cross-checked twice.

Each non-error case records ``digest_python`` (hashlib over the hand-written bytes) and
``digest_sha256sum`` (``sha256sum`` over the same bytes written to a file outside the repository).
"""

from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path
from typing import Any

import pytest

from studio.domain.canonical import canonical_bytes, format_number
from studio.domain.errors import DomainValidationError

GOLDEN = Path(__file__).resolve().parent / "golden" / "fingerprint_v2.json"
DOC: dict[str, Any] = json.loads(GOLDEN.read_text(encoding="utf-8"))

# Vector ids that must be present at this point of the implementation (design B.6 numbering).
REQUIRED_IDS = {8, 10, 14}


def _cases() -> list[tuple[int, dict[str, Any]]]:
    return [(v["id"], case) for v in DOC["vectors"] for case in v["cases"]]


def _produce(spec: dict[str, Any]) -> bytes:
    kind = spec["type"]
    if kind == "value":
        return canonical_bytes(spec["value"])
    if kind == "float_hex":
        return canonical_bytes(struct.unpack(">d", bytes.fromhex(spec["hex"]))[0])
    raise AssertionError(f"unknown golden input type {kind!r}")


def test_golden_header() -> None:
    assert DOC["canonical_version"] == 2
    assert DOC["alg"] == "sha256"


def test_required_vectors_present() -> None:
    ids = [v["id"] for v in DOC["vectors"]]
    assert len(ids) == len(set(ids))
    assert REQUIRED_IDS <= set(ids)


@pytest.mark.parametrize(("vector_id", "case"), _cases(), ids=lambda x: str(x)[:40])
def test_golden_case(vector_id: int, case: dict[str, Any]) -> None:
    if "error" in case:
        with pytest.raises(DomainValidationError) as err:
            _produce(case["input"])
        assert err.value.issues[0].code == case["error"]
        return
    expected = case["canonical_bytes"].encode("utf-8")
    assert _produce(case["input"]) == expected
    digest = hashlib.sha256(expected).hexdigest()
    assert case["digest_python"] == digest
    assert case["digest_sha256sum"] == digest


def test_rfc8785_samples_are_independently_derivable() -> None:
    vector = next(v for v in DOC["vectors"] if v["id"] == 14)
    assert len(vector["cases"]) >= 20
    for case in vector["cases"]:
        hexval = case["input"]["hex"]
        text = case["canonical_bytes"]
        raw = bytes.fromhex(hexval)
        x = struct.unpack(">d", raw)[0]
        # The text denotes exactly this double (bit-identical, except that minus zero prints 0).
        assert struct.pack(">d", float(text)) == raw or (x == 0 and float(text) == 0)
        assert format_number(x) == text
        digits = case["derivation"].get("digits")
        if digits is not None and len(digits) > 1:
            shorter = f"{abs(x):.{len(digits) - 2}e}"
            assert float(shorter) != abs(x), "a shorter digit string also round-trips"
