"""C.2 registry recovery block, REG-3 managed_environment and the REG-6 constants (schema level)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from studio.registry import (
    EXECUTION_ONLY_CREATIVE_ALLOWLIST,
    RESERVED_CREATIVE_FIELDS,
    BindingSpec,
    EngineSpec,
    RecoveryPolicy,
    RegistryError,
    ResolvedRecovery,
    load_registry,
)

FIELDS = ("max_infrastructure_retries", "max_resumes", "max_resume_depth")


# --- the RecoveryPolicy block -------------------------------------------------------------------


def test_policy_has_no_default_numbers():
    p = RecoveryPolicy()
    assert (p.max_infrastructure_retries, p.max_resumes, p.max_resume_depth) == (None, None, None)


def test_policy_field_names_are_the_shared_contract():
    assert tuple(RecoveryPolicy.model_fields) == FIELDS


@pytest.mark.parametrize("value", [0, 1, 3, 100, None])
def test_retries_and_resumes_accept_non_negative_integers_and_none(value: int | None):
    p = RecoveryPolicy(max_infrastructure_retries=value, max_resumes=value)
    assert (p.max_infrastructure_retries, p.max_resumes) == (value, value)


@pytest.mark.parametrize("value", [1, 2, 3, 100, None])
def test_resume_depth_accepts_one_or_more_and_none(value: int | None):
    assert RecoveryPolicy(max_resume_depth=value).max_resume_depth == value


@pytest.mark.parametrize("value", [0, -1])
def test_r9_resume_depth_below_one_is_rejected(value: int):
    with pytest.raises(ValidationError):
        RecoveryPolicy(max_resume_depth=value)


def test_r9_zero_is_still_valid_for_retries_and_resumes_but_not_for_depth():
    assert RecoveryPolicy(max_infrastructure_retries=0, max_resumes=0).max_resumes == 0
    with pytest.raises(ValidationError):
        RecoveryPolicy(max_resumes=0, max_resume_depth=0)


def test_r9_a_zero_depth_is_refused_when_loading_a_registry(specs: Any, reg_root: Path):
    for where in ("engine", "binding"):
        block = {"max_resume_depth": 0}
        eng = specs.engine(**({"recovery": block} if where == "engine" else {}))
        bnd = specs.binding(**({"recovery": block} if where == "binding" else {}))
        with pytest.raises(RegistryError) as info:
            load_registry(specs.write(reg_root / where, engines=[eng], bindings=[bnd]))
        assert any(
            i.code == "schema" and "max_resume_depth" in i.subject for i in info.value.issues
        )


@pytest.mark.parametrize("value", [-1, True, False, 1.5, "3", [1]])
def test_policy_rejects_everything_else(value: Any):
    for field in FIELDS:
        with pytest.raises(ValidationError):
            RecoveryPolicy(**{field: value})


def test_policy_rejects_unknown_keys_and_is_frozen():
    with pytest.raises(ValidationError):
        RecoveryPolicy(max_retries=2)  # type: ignore[call-arg]
    p = RecoveryPolicy(max_resumes=1)
    with pytest.raises(ValidationError):
        p.max_resumes = 2  # type: ignore[misc]


def test_blocks_are_optional_on_engines_and_bindings(specs: Any):
    assert EngineSpec.model_validate(specs.engine()).recovery is None
    assert BindingSpec.model_validate(specs.binding()).recovery is None


def test_blocks_parse_on_engines_and_bindings(specs: Any):
    e = EngineSpec.model_validate(specs.engine(recovery={"max_infrastructure_retries": 4}))
    b = BindingSpec.model_validate(
        specs.binding(recovery={"max_resumes": 1, "max_resume_depth": 2})
    )
    assert e.recovery == RecoveryPolicy(max_infrastructure_retries=4)
    assert b.recovery == RecoveryPolicy(max_resumes=1, max_resume_depth=2)


# --- override semantics ---------------------------------------------------------------------------


def _load(specs: Any, root: Path, *, engine: dict[str, Any] | None, binding: dict[str, Any] | None):
    eng = specs.engine(**({"recovery": engine} if engine is not None else {}))
    bnd = specs.binding(**({"recovery": binding} if binding is not None else {}))
    return load_registry(specs.write(root, engines=[eng], bindings=[bnd]))


def test_neither_block_means_no_automatic_recovery(specs: Any, reg_root: Path):
    r = _load(specs, reg_root, engine=None, binding=None).resolve_recovery("eng.b1")
    assert r == ResolvedRecovery(source="none")
    assert (r.max_infrastructure_retries, r.max_resumes, r.max_resume_depth) == (None, None, None)


def test_engine_block_applies_when_the_binding_has_none(specs: Any, reg_root: Path):
    reg = _load(
        specs,
        reg_root,
        engine={"max_infrastructure_retries": 3, "max_resumes": 2, "max_resume_depth": 1},
        binding=None,
    )
    assert reg.resolve_recovery("eng.b1") == ResolvedRecovery("engine:eng", 3, 2, 1)


def test_binding_block_applies_without_an_engine_block(specs: Any, reg_root: Path):
    reg = _load(specs, reg_root, engine=None, binding={"max_resumes": 5})
    assert reg.resolve_recovery("eng.b1") == ResolvedRecovery("binding:eng.b1", None, 5, None)


def test_binding_block_replaces_the_engine_block_whole_without_merging(specs: Any, reg_root: Path):
    reg = _load(
        specs,
        reg_root,
        engine={"max_infrastructure_retries": 3, "max_resumes": 2, "max_resume_depth": 1},
        binding={"max_resumes": 7},
    )
    r = reg.resolve_recovery("eng.b1")
    assert r.source == "binding:eng.b1"
    assert r.max_resumes == 7
    assert r.max_infrastructure_retries is None  # NOT inherited from the engine
    assert r.max_resume_depth is None  # NOT inherited from the engine


def test_an_empty_binding_block_still_replaces_the_engine_block(specs: Any, reg_root: Path):
    reg = _load(specs, reg_root, engine={"max_infrastructure_retries": 3}, binding={})
    r = reg.resolve_recovery("eng.b1")
    assert r.source == "binding:eng.b1" and r.max_infrastructure_retries is None


def test_override_is_per_binding(specs: Any, reg_root: Path):
    b1 = specs.binding()
    b2 = specs.binding(id="eng.b2", recovery={"max_infrastructure_retries": 0})
    reg = load_registry(
        specs.write(
            reg_root,
            engines=[specs.engine(recovery={"max_infrastructure_retries": 9})],
            bindings=[b1, b2],
        )
    )
    assert reg.resolve_recovery("eng.b1").max_infrastructure_retries == 9
    assert reg.resolve_recovery("eng.b2") == ResolvedRecovery("binding:eng.b2", 0, None, None)


def test_unknown_binding_id_raises_keyerror(specs: Any, reg_root: Path):
    reg = _load(specs, reg_root, engine=None, binding=None)
    with pytest.raises(KeyError):
        reg.resolve_recovery("nope")


# --- managed_environment (REG-3, schema side) -----------------------------------------------------


def test_managed_environment_defaults_to_empty_and_parses(specs: Any):
    assert BindingSpec.model_validate(specs.binding()).managed_environment == ()
    b = BindingSpec.model_validate(specs.binding(managed_environment=["TEMP", "HF_HOME", "_x1"]))
    assert b.managed_environment == ("TEMP", "HF_HOME", "_x1")


@pytest.mark.parametrize("name", ["", "1X", "A-B", "A B", "A=B", "ÄB", "A" * 129])
def test_managed_environment_rejects_bad_variable_names(specs: Any, name: str):
    with pytest.raises(ValidationError):
        BindingSpec.model_validate(specs.binding(managed_environment=[name]))


# --- REG-6 constants -----------------------------------------------------------------------------


def test_allowlist_is_exactly_the_ud6_examples_and_never_fps():
    assert EXECUTION_ONLY_CREATIVE_ALLOWLIST == {"width", "height", "duration_s"}
    assert "fps" not in EXECUTION_ONLY_CREATIVE_ALLOWLIST


def test_allowlist_is_a_subset_of_the_reserved_creative_fields():
    assert EXECUTION_ONLY_CREATIVE_ALLOWLIST < RESERVED_CREATIVE_FIELDS
    assert {"prompt", "seed", "fps", "references", "operation"} <= RESERVED_CREATIVE_FIELDS
