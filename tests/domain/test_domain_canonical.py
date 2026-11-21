"""Canonical form v2 (RFC 8785 over a restricted value domain)."""

from __future__ import annotations

import string
from enum import StrEnum
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from studio.domain.canonical import (
    CANONICAL_VERSION,
    SAFE_INT_MAX,
    canonical_bytes,
    canonical_json,
    format_number,
    freeze_params,
    freeze_value,
)
from studio.domain.errors import DomainValidationError


def _code(excinfo: pytest.ExceptionInfo[DomainValidationError]) -> str:
    return excinfo.value.issues[0].code


def _digits(text: str) -> str:
    mantissa = text.lstrip("-").split("e")[0]
    return mantissa.replace(".", "").strip("0")


def test_version_is_two() -> None:
    assert CANONICAL_VERSION == 2


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (0.0, "0"),
        (-0.0, "0"),
        (1.0, "1"),
        (24.0, "24"),
        (100.0, "100"),
        (0.1, "0.1"),
        (1.5, "1.5"),
        (-2.5, "-2.5"),
        (123.456, "123.456"),
        (0.000001, "0.000001"),
        (1e-7, "1e-7"),
        (1.5e-7, "1.5e-7"),
        (1e20, "100000000000000000000"),
        (1e21, "1e+21"),
        (1.5e300, "1.5e+300"),
        (5e-324, "5e-324"),
        (1.7976931348623157e308, "1.7976931348623157e+308"),
        (2.0**53, "9007199254740992"),
    ],
)
def test_format_number_known_values(value: float, text: str) -> None:
    assert format_number(value) == text


@given(st.floats(allow_nan=False, allow_infinity=False))
def test_format_number_round_trips_and_is_shortest(x: float) -> None:
    text = format_number(x)
    assert float(text) == x
    digits = _digits(text)
    if x != 0 and len(digits) > 1:
        shorter = f"{abs(x):.{len(digits) - 2}e}"
        assert float(shorter) != abs(x)


def test_format_number_rejects_non_finite() -> None:
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(DomainValidationError) as err:
            format_number(bad)
        assert _code(err) == "canonical.non_finite"


def test_integer_and_float_of_equal_value_share_bytes() -> None:
    assert canonical_bytes({"a": 24}) == canonical_bytes({"a": 24.0})
    assert canonical_json({"a": 24.0}) == '{"a":24}'


def test_bool_is_not_a_number() -> None:
    assert canonical_json([True, 1, False, 0]) == "[true,1,false,0]"


def test_negative_zero_is_zero() -> None:
    assert canonical_json({"zero": -0.0}) == '{"zero":0}'


@pytest.mark.parametrize("value", [SAFE_INT_MAX, -SAFE_INT_MAX, 0])
def test_integer_range_accepted(value: int) -> None:
    assert canonical_json(value) == str(value)


@pytest.mark.parametrize("value", [SAFE_INT_MAX + 1, -SAFE_INT_MAX - 1, 2**64 - 1])
def test_integer_range_rejected(value: int) -> None:
    with pytest.raises(DomainValidationError) as err:
        canonical_json(value)
    assert _code(err) == "canonical.int_range"


def test_non_finite_values_rejected() -> None:
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(DomainValidationError) as err:
            canonical_json({"x": bad})
        assert _code(err) == "canonical.non_finite"


def test_keys_must_be_ascii_strings() -> None:
    with pytest.raises(DomainValidationError) as err:
        canonical_json({"café": 1})
    assert _code(err) == "canonical.key_ascii"
    with pytest.raises(DomainValidationError) as err2:
        canonical_json({1: "x"})
    assert _code(err2) == "canonical.key_type"


def test_keys_sorted_by_code_point() -> None:
    assert canonical_json({"b": 1, "a": 2, "B": 3}) == '{"B":3,"a":2,"b":1}'


def test_lone_surrogate_rejected() -> None:
    with pytest.raises(DomainValidationError) as err:
        canonical_json("\ud800")
    assert _code(err) == "canonical.bad_string"


@pytest.mark.parametrize("bad", [{1, 2}, b"bytes", object()])
def test_unsupported_types_rejected(bad: Any) -> None:
    with pytest.raises(DomainValidationError) as err:
        canonical_json(bad)
    assert _code(err) == "canonical.unsupported_type"


def test_string_escaping_matches_jcs() -> None:
    assert canonical_json('a"b\\c\n\u0001') == '"a\\"b\\\\c\\n\\u0001"'
    assert canonical_json(" é\x7f") == '" é\x7f"'


def test_unicode_is_not_normalized() -> None:
    assert canonical_bytes("é") != canonical_bytes("é")


def test_enums_become_their_value() -> None:
    class Color(StrEnum):
        RED = "red"

    assert canonical_json({"c": Color.RED}) == '{"c":"red"}'


def test_sequences_keep_order_and_tuples_equal_lists() -> None:
    assert canonical_json([3, 1, 2]) == "[3,1,2]"
    assert canonical_bytes((1, 2)) == canonical_bytes([1, 2])


def test_frozen_mapping_is_read_only() -> None:
    frozen = freeze_value({"a": [1, {"b": 2}]})
    with pytest.raises(TypeError):
        frozen["c"] = 1
    assert isinstance(frozen["a"], tuple)


def test_freeze_params_checks_keys() -> None:
    assert dict(freeze_params(None, "p")) == {}
    with pytest.raises(DomainValidationError) as err:
        freeze_params({"1bad": 1}, "p")
    assert _code(err) == "params.key"
    with pytest.raises(DomainValidationError) as err2:
        freeze_params([1], "p")
    assert _code(err2) == "params.type"


_KEYS = st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=4)


@given(
    st.dictionaries(_KEYS, st.integers(-100, 100), max_size=8),
    st.randoms(use_true_random=False),
)
def test_mapping_permutation_does_not_change_bytes(d: dict[str, int], rnd: Any) -> None:
    items = list(d.items())
    rnd.shuffle(items)
    assert canonical_bytes(dict(items)) == canonical_bytes(d)


class _LyingInt(int):
    """An int subclass whose text and repr are not its value (B-11 C4)."""

    def __str__(self) -> str:
        return "007"

    def __repr__(self) -> str:
        return "int-is-a-lie"


def test_int_subclass_cannot_change_the_bytes() -> None:
    plain = {"a": 7, "b": [7, {"c": -7}]}
    lying = {"a": _LyingInt(7), "b": [_LyingInt(7), {"c": _LyingInt(-7)}]}
    assert canonical_bytes(lying) == canonical_bytes(plain) == b'{"a":7,"b":[7,{"c":-7}]}'
    frozen = freeze_value(_LyingInt(7))
    assert type(frozen) is int and frozen == 7


def test_int_subclass_is_still_range_checked() -> None:
    with pytest.raises(DomainValidationError) as err:
        canonical_bytes({"a": _LyingInt(SAFE_INT_MAX + 1)})
    assert _code(err) == "canonical.int_range"
    assert canonical_json(_LyingInt(SAFE_INT_MAX)) == str(SAFE_INT_MAX)


def test_integral_float_and_equal_integer_share_one_text_and_out_of_range_ints_are_rejected() -> (
    None
):
    """R-12: floats follow RFC 8785, integers are limited to +-(2**53 - 1); there is no way to write two
    encodings of one value because an integer beyond the range cannot be written at all."""
    assert canonical_json(2.0**53) == canonical_json(2**53 - 1 + 1.0) == "9007199254740992"
    with pytest.raises(DomainValidationError):
        canonical_json(2**53)


@given(st.floats(allow_nan=False, allow_infinity=False))
def test_float_value_text_is_stable_under_freezing(x: float) -> None:
    assert canonical_json({"x": x}) == '{"x":' + format_number(x) + "}"
