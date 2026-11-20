"""REG-1: static compatibility naming, and the rules behind it (unknown never passes)."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import studio.registry
from studio.config.settings import StudioSettings
from studio.registry import (
    BindingVerdict,
    check_hardware_compatibility,
    check_static_compatibility,
    evaluate_registry,
    load_registry,
)
from studio.registry import compat as compat_module

PROFILE = """\
schema_version = 1
name = "unit-hw"
gpu_vram_total_gb = 6.0
{extra}
provisional = true

[thresholds]
vram_margin_gb = 0.3
default_cuda_context_mb = 400
ram_reserve_gb = 4.0
commit_reserve_gb = 2.0
"""
KNOWN_HW = "system_ram_gb = 32.0\ncommit_limit_gb = 40.0"


def q(value: float | None) -> dict[str, Any]:
    if value is None:
        return {"value": None, "basis": "unknown"}
    return {"value": value, "basis": "observed", "source": "unit test"}


def res(
    vram: float | None, ram: float | None, commit: float | None, **extra: Any
) -> dict[str, Any]:
    return {
        "gpu": "exclusive",
        "vram_gb": q(vram),
        "ram_gb": q(ram),
        "commit_gb": q(commit),
        **extra,
    }


@pytest.fixture
def verdict(
    specs: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Callable[..., BindingVerdict]:
    def run(
        resources: dict[str, Any], *, hw: str = KNOWN_HW, kind: str = "process"
    ) -> BindingVerdict:
        cfg = tmp_path / "cfg"
        (cfg / "hardware").mkdir(parents=True, exist_ok=True)
        (cfg / "hardware" / "unit-hw.toml").write_text(PROFILE.format(extra=hw), encoding="utf-8")
        monkeypatch.setenv("STUDIO_CONFIG_DIR", str(cfg))
        settings = StudioSettings.model_validate({"env": "dev", "hardware_profile": "unit-hw"})
        reg = load_registry(
            specs.write(
                tmp_path / "reg",
                engines=[specs.engine(kind=kind)],
                bindings=[specs.binding(resources=resources)],
            )
        )
        return check_static_compatibility(reg, "eng.b1", settings)

    return run


# --- the rename ------------------------------------------------------------------------------------------


def test_the_new_names_exist_and_the_misleading_old_ones_are_gone():
    assert studio.registry.check_static_compatibility is check_static_compatibility
    assert "check_static_compatibility" in studio.registry.__all__
    assert not hasattr(studio.registry, "is_admissible")
    assert "is_admissible" not in studio.registry.__all__
    assert not hasattr(compat_module, "is_admissible")


def test_the_verdict_field_is_statically_compatible():
    names = [f.name for f in dataclasses.fields(BindingVerdict)]
    assert "statically_compatible" in names and "admissible" not in names


# --- unknown never passes ------------------------------------------------------------------------------


def test_everything_known_and_fitting_is_statically_compatible(verdict: Callable[..., Any]):
    v = verdict(res(5.0, 8.0, 10.0))
    assert v.statically_compatible is True and v.codes == ()
    assert v.required_vram_gb == pytest.approx(5.0 + 400 / 1024 + 0.3)


@pytest.mark.parametrize(
    "resources, expected",
    [
        (res(None, 8.0, 10.0), ["vram_unknown"]),
        (res(5.0, None, 10.0), ["ram_unknown"]),
        (res(5.0, 8.0, None), ["commit_unknown"]),
        (res(None, None, None), ["vram_unknown", "ram_unknown", "commit_unknown"]),
    ],
)
def test_any_unknown_requirement_blocks(
    verdict: Callable[..., Any], resources: dict[str, Any], expected: list[str]
):
    v = verdict(resources)
    assert v.statically_compatible is False
    assert list(v.codes) == expected


def test_an_unknown_profile_ram_or_commit_blocks(verdict: Callable[..., Any]):
    v = verdict(res(5.0, 8.0, 10.0), hw="")
    assert not v.statically_compatible
    assert set(v.codes) == {"profile_ram_unknown", "profile_commit_unknown"}


def test_a_cpu_binding_needs_no_vram_but_still_needs_ram_and_commit(
    specs: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    cfg = tmp_path / "cfg"
    (cfg / "hardware").mkdir(parents=True)
    (cfg / "hardware" / "unit-hw.toml").write_text(PROFILE.format(extra=KNOWN_HW), encoding="utf-8")
    monkeypatch.setenv("STUDIO_CONFIG_DIR", str(cfg))
    settings = StudioSettings.model_validate({"env": "dev", "hardware_profile": "unit-hw"})
    cpu = specs.binding(
        id="tool.cpu",
        engine="tool",
        operation="transcode",
        inputs=[{"role": "clip", "media_type": "video", "required": True}],
        resources={"gpu": "none", "ram_gb": q(None), "commit_gb": q(None)},
    )
    engine = specs.engine(id="tool", kind="tool", operations=["transcode"])
    reg = load_registry(specs.write(tmp_path / "reg", engines=[engine], bindings=[cpu]))
    v = check_static_compatibility(reg, "tool.cpu", settings)
    assert v.required_vram_gb == 0.0
    assert set(v.codes) == {"ram_unknown", "commit_unknown"}


# --- exceeding limits ------------------------------------------------------------------------------------


def test_vram_over_the_nominal_total_blocks(verdict: Callable[..., Any]):
    v = verdict(res(5.5, 8.0, 10.0))  # 5.5 + 0.39 context + 0.3 margin > 6.0
    assert list(v.codes) == ["exceeds_total_vram"]


def test_the_context_term_depends_on_the_engine_and_the_estimate(verdict: Callable[..., Any]):
    assert not verdict(res(5.5, 8.0, 10.0)).statically_compatible
    # a resident service already holds its CUDA context
    assert verdict(res(5.5, 8.0, 10.0), kind="service").statically_compatible
    # an estimate that already includes the context is not charged twice
    assert verdict(res(5.5, 8.0, 10.0, estimate_includes_context=True)).statically_compatible
    # ...but an estimate that excludes it is
    assert not verdict(res(5.5, 8.0, 10.0, estimate_includes_context=False)).statically_compatible


@pytest.mark.parametrize(
    "resources, code",
    [(res(5.0, 29.0, 10.0), "exceeds_ram"), (res(5.0, 8.0, 39.0), "exceeds_commit")],
)
def test_ram_and_commit_reserves_are_applied(
    verdict: Callable[..., Any], resources: dict[str, Any], code: str
):
    assert list(verdict(resources).codes) == [code]


def test_exactly_at_the_limit_still_fits(verdict: Callable[..., Any]):
    assert verdict(res(5.0, 28.0, 38.0)).statically_compatible  # 28+4 == 32, 38+2 == 40


# --- registry-level functions ------------------------------------------------------------------------------


def test_unknown_binding_ids_raise_keyerror_and_never_pass(
    specs: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    cfg = tmp_path / "cfg"
    (cfg / "hardware").mkdir(parents=True)
    (cfg / "hardware" / "unit-hw.toml").write_text(PROFILE.format(extra=KNOWN_HW), encoding="utf-8")
    monkeypatch.setenv("STUDIO_CONFIG_DIR", str(cfg))
    settings = StudioSettings.model_validate({"env": "dev", "hardware_profile": "unit-hw"})
    reg = load_registry(specs.write(tmp_path / "reg"))
    with pytest.raises(KeyError):
        check_static_compatibility(reg, "ghost", settings)
    assert set(evaluate_registry(reg, settings)) == {"eng.b1"}


def test_issue_messages_use_the_new_wording_and_the_provisional_warning(
    specs: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    cfg = tmp_path / "cfg"
    (cfg / "hardware").mkdir(parents=True)
    (cfg / "hardware" / "unit-hw.toml").write_text(PROFILE.format(extra=KNOWN_HW), encoding="utf-8")
    monkeypatch.setenv("STUDIO_CONFIG_DIR", str(cfg))
    settings = StudioSettings.model_validate({"env": "dev", "hardware_profile": "unit-hw"})
    reg = load_registry(specs.write(tmp_path / "reg"))  # every requirement unknown
    issues = check_hardware_compatibility(reg, settings)
    errors = [i for i in issues if i.severity == "error"]
    assert {i.code for i in errors} == {"vram_unknown", "ram_unknown", "commit_unknown"}
    assert all("not statically compatible on 'unit-hw'" in i.message for i in errors)
    assert all("admissible" not in i.message for i in issues)
    assert [i.code for i in issues if i.severity == "warning"] == ["thresholds_provisional"]


def test_loader_warnings_no_longer_call_unknown_resources_admissible(specs: Any, tmp_path: Path):
    reg = load_registry(specs.write(tmp_path / "reg"))
    unknowns = [i for i in reg.warnings if i.code.endswith("_unknown")]
    assert unknowns and all("statically compatible" in i.message for i in unknowns)
    assert all("admissible" not in i.message for i in unknowns)
