"""UD-4: no shared budget of 2. No symbol, default or literal 2 stands for a budget anywhere in the domain."""

from __future__ import annotations

import ast
import dataclasses
import re
from pathlib import Path

import studio.domain as domain

DOMAIN_DIR = Path(__file__).resolve().parents[2] / "src" / "studio" / "domain"
BUDGET_FIELD = re.compile(r"^max_|budget|retries$")
BUDGET_NAME = re.compile(r"(?i)default.*(retr|resum|budget)|(retr|resum|budget).*default")


def _modules() -> list[tuple[Path, ast.Module]]:
    return [(p, ast.parse(p.read_text(encoding="utf-8"))) for p in sorted(DOMAIN_DIR.glob("*.py"))]


def _is_int_two(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and type(node.value) is int and node.value == 2


def test_the_old_default_is_gone() -> None:
    assert not hasattr(domain, "DEFAULT_MAX_INFRASTRUCTURE_RETRIES")
    for path, tree in _modules():
        text = path.read_text(encoding="utf-8")
        assert "DEFAULT_MAX_INFRASTRUCTURE_RETRIES" not in text, path.name
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Name):
                names.append(node.id)
            elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                names.append(node.name)
            elif isinstance(node, ast.arg):
                names.append(node.arg)
            for name in names:
                assert not BUDGET_NAME.search(name), f"{path.name}: {name}"


def test_no_literal_two_in_the_domain() -> None:
    """Every integer literal 2 is a violation, except the base of ``2**n`` (the safe-integer and seed bounds)
    and the single canonical-form version constant."""
    offenders: list[str] = []
    for path, tree in _modules():
        parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and _is_int_two(node)):
                continue
            parent = parents[node]
            if (
                isinstance(parent, ast.BinOp)
                and isinstance(parent.op, ast.Pow)
                and parent.left is node
            ):
                continue
            if (
                isinstance(parent, ast.Assign)
                and len(parent.targets) == 1
                and isinstance(parent.targets[0], ast.Name)
                and parent.targets[0].id == "CANONICAL_VERSION"
            ):
                continue
            offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == []


def test_budget_fields_have_no_default() -> None:
    checked: list[str] = []
    for name in dir(domain):
        cls = getattr(domain, name)
        if not (isinstance(cls, type) and dataclasses.is_dataclass(cls)):
            continue
        for f in dataclasses.fields(cls):
            if BUDGET_FIELD.search(f.name):
                checked.append(f"{cls.__name__}.{f.name}")
                assert f.default is dataclasses.MISSING, f"{cls.__name__}.{f.name}"
                assert f.default_factory is dataclasses.MISSING, f"{cls.__name__}.{f.name}"
    assert "RecoveryPolicySnapshot.max_infrastructure_retries" in checked
    assert "RecoveryPolicySnapshot.max_resumes" in checked
    assert "RecoveryPolicySnapshot.max_resume_depth" in checked


def test_job_has_no_budget_field_of_its_own() -> None:
    names = {f.name: f for f in dataclasses.fields(domain.Job)}
    assert not [n for n in names if BUDGET_FIELD.search(n) and n != "recovery_policy"]
    assert names["recovery_policy"].default is dataclasses.MISSING
    assert names["recovery_policy"].default_factory is dataclasses.MISSING
