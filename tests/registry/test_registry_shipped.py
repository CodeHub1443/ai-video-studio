"""The registry that ships in config/registry (read-only)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from studio.registry import (
    EXECUTION_ONLY_CREATIVE_ALLOWLIST,
    RESERVED_CREATIVE_FIELDS,
    Registry,
    load_registry,
)

SHIPPED = Path(__file__).resolve().parents[2] / "config" / "registry"


@pytest.fixture(scope="module")
def shipped() -> Registry:
    return load_registry(SHIPPED)


def test_shipped_registry_loads_without_errors(shipped: Registry):
    assert {"comfyui", "ffmpeg", "ltx_streaming"} <= set(shipped.engines)
    assert len(shipped.bindings) == 4


def test_no_shipped_yaml_has_a_recovery_block(shipped: Registry):
    """M0.3a: limits stay unset until the user decides (UD-7, I-8); no automatic recovery meanwhile."""
    for engine in shipped.engines.values():
        assert engine.recovery is None, engine.id
    for binding in shipped.bindings.values():
        assert binding.recovery is None, binding.id
        assert shipped.resolve_recovery(binding.id).source == "none"


def test_ffmpeg_transcode_no_longer_claims_bitwise_determinism(shipped: Registry):
    b = shipped.bindings["ffmpeg.cpu.transcode"]
    assert b.determinism.value == "perceptual"
    assert b.execution_only == ("threads",)
    assert "determinism_execution_only" not in [i.code for i in shipped.warnings]


@pytest.mark.parametrize("binding_id", ["ltx_streaming.ltx25.i2v", "ltx_streaming.ltx25.t2v"])
def test_ltx_bindings_separate_execution_parameters_from_managed_environment(
    shipped: Registry, binding_id: str
):
    b = shipped.bindings[binding_id]
    assert b.execution_only == ("SYNC_S1",)
    assert b.managed_environment == ("TEMP", "TMP", "HF_HOME")
    assert not {k.casefold() for k in b.execution_only} & {
        k.casefold() for k in b.managed_environment
    }


def test_no_shipped_binding_trips_the_new_loader_checks(shipped: Registry):
    new_codes = {
        "determinism_execution_only",
        "managed_environment_conflict",
        "execution_only_reserved",
        "ignored_entry",
    }
    assert new_codes.isdisjoint(i.code for i in shipped.warnings)
    for b in shipped.bindings.values():
        assert not set(b.execution_only) & (
            RESERVED_CREATIVE_FIELDS - EXECUTION_ONLY_CREATIVE_ALLOWLIST
        )


def test_no_shipped_binding_file_still_says_admissible():
    """C-7 / REG-1: the static check is 'statically compatible', not runtime admission (ADR-0007)."""
    files = sorted((SHIPPED / "bindings").glob("*.yaml"))
    assert len(files) == 4
    offenders = [
        f.name
        for f in files
        if re.search(r"\badmissible\b", f.read_text(encoding="utf-8"), re.IGNORECASE)
    ]
    assert offenders == []


def test_the_shipped_engine_and_model_files_are_unchanged_by_this_brief():
    """Guard against accidental edits: the engine and model YAML only list what they always listed."""
    engines = sorted(p.name for p in (SHIPPED / "engines").glob("*.yaml"))
    models = sorted(p.name for p in (SHIPPED / "models").glob("*.yaml"))
    assert engines == ["comfyui.yaml", "ffmpeg.yaml", "ltx_streaming.yaml"]
    assert models == ["ae_vae.yaml", "ltx25_22b.yaml", "qwen3_4b_q8.yaml", "z_image_turbo_q5.yaml"]
