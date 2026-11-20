"""The registry that ships in config/registry (read-only)."""

from __future__ import annotations

from pathlib import Path

import pytest

from studio.registry import Registry, load_registry

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
