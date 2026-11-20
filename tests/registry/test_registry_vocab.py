"""REG-4 / ADR-0010 P-6: the vocabulary has one source, studio.domain.vocab."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import studio.registry
from studio.domain import vocab
from studio.registry import schema

SRC = Path(__file__).resolve().parents[2] / "src" / "studio"
SHARED = ("Operation", "MediaType", "CommercialUse")


@pytest.mark.parametrize("name", SHARED)
def test_registry_uses_the_domain_enum_objects(name: str):
    assert getattr(schema, name) is getattr(vocab, name)


def test_package_level_operation_is_the_domain_enum():
    assert studio.registry.Operation is vocab.Operation


@pytest.mark.parametrize("name", SHARED)
def test_enum_members_are_frozen_contract(name: str):
    expected = {
        "Operation": {
            "t2i", "i2i", "inpaint", "t2v", "i2v", "v2v", "upscale", "interp", "tts", "music",
            "sfx", "asr", "transcode", "render_timeline",
        },
        "MediaType": {"image", "video", "audio", "text", "mask", "timeline"},
        "CommercialUse": {"yes", "conditional", "no", "unknown"},
    }[name]  # fmt: skip
    assert {m.value for m in getattr(vocab, name)} == expected


def test_no_duplicate_vocabulary_class_in_the_registry_package():
    offenders: list[str] = []
    for file in sorted((SRC / "registry").glob("*.py")):
        tree = ast.parse(file.read_text(encoding="utf-8"))
        offenders += [
            f"{file.name}:{node.name}"
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and node.name in SHARED
        ]
    assert offenders == []


def test_the_domain_never_imports_the_registry_or_pydantic():
    bad: list[str] = []
    for file in sorted((SRC / "domain").glob("*.py")):
        for node in ast.walk(ast.parse(file.read_text(encoding="utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module]
            bad += [
                f"{file.name}:{n}"
                for n in names
                if n.startswith(("studio.registry", "studio.config", "pydantic"))
            ]
    assert bad == []
