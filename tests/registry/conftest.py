"""Builders for throw-away registry trees (JSON is valid YAML, so files are written with ``json``)."""

from __future__ import annotations

import copy
import json
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

Spec = dict[str, Any]

_ENGINE: Spec = {
    "schema_version": 1,
    "id": "eng",
    "display_name": "Test engine",
    "kind": "process",
    "adapter": "studio.engines.eng",
    "operations": ["t2v"],
}
_BINDING: Spec = {
    "schema_version": 1,
    "id": "eng.b1",
    "display_name": "Test binding",
    "engine": "eng",
    "operation": "t2v",
    "models": [],
    "inputs": [],
    "outputs": [{"role": "video", "media_type": "video"}],
    "resources": {
        "gpu": "exclusive",
        "vram_gb": {"value": None, "basis": "unknown"},
        "ram_gb": {"value": None, "basis": "unknown"},
        "commit_gb": {"value": None, "basis": "unknown"},
    },
}
_MODEL: Spec = {
    "schema_version": 1,
    "id": "m1",
    "display_name": "Test model",
    "kind": "other",
    "family": "test",
    "format": "safetensors",
    "license": {"id": "test"},
    "files": [{"root": "models", "path": "m1.safetensors", "role": "weights"}],
}


class Specs:
    """Valid default specs plus a writer. Every builder returns a fresh deep copy."""

    @staticmethod
    def engine(**over: Any) -> Spec:
        return {**copy.deepcopy(_ENGINE), **over}

    @staticmethod
    def binding(**over: Any) -> Spec:
        return {**copy.deepcopy(_BINDING), **over}

    @staticmethod
    def model(**over: Any) -> Spec:
        return {**copy.deepcopy(_MODEL), **over}

    @staticmethod
    def write(
        root: Path,
        *,
        engines: Iterable[Spec] | None = None,
        models: Iterable[Spec] = (),
        bindings: Iterable[Spec] | None = None,
    ) -> Path:
        """Write ``<root>/{engines,models,bindings}/<id>.yaml``; defaults give one valid engine + binding."""
        groups = {
            "engines": list(engines) if engines is not None else [Specs.engine()],
            "models": list(models),
            "bindings": list(bindings) if bindings is not None else [Specs.binding()],
        }
        for section, specs in groups.items():
            folder = root / section
            folder.mkdir(parents=True, exist_ok=True)
            for spec in specs:
                (folder / f"{str(spec['id']).replace(':', '_')}.yaml").write_text(
                    json.dumps(spec), encoding="utf-8"
                )
        return root


@pytest.fixture(autouse=True)
def _clean_studio_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Registry tests must not see the developer's environment (same rule as tests/config)."""
    for name in list(os.environ):
        if name.startswith("STUDIO_"):
            monkeypatch.delenv(name)


@pytest.fixture
def specs() -> type[Specs]:
    return Specs


@pytest.fixture
def reg_root(tmp_path: Path) -> Path:
    return tmp_path / "registry"
