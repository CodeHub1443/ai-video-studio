"""Domain boundary: ``studio.domain`` imports only the standard library and itself."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

DOMAIN_DIR = Path(__file__).resolve().parents[2] / "src" / "studio" / "domain"


def _sources() -> list[Path]:
    return sorted(DOMAIN_DIR.glob("*.py"))


def _imports(tree: ast.AST) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.append(("." * node.level + (node.module or ""), node.lineno))
    return found


def test_domain_sources_exist() -> None:
    assert DOMAIN_DIR.is_dir()
    assert len(_sources()) >= 10


def test_domain_imports_only_stdlib_and_itself() -> None:
    offenders: list[str] = []
    for path in _sources():
        for name, line in _imports(ast.parse(path.read_text(encoding="utf-8"))):
            top = name.split(".")[0]
            own = name == "studio.domain" or name.startswith("studio.domain.")
            if not own and top not in sys.stdlib_module_names:
                offenders.append(f"{path.name}:{line} imports {name}")
    assert offenders == []


def test_domain_has_no_dynamic_imports() -> None:
    offenders: list[str] = []
    for path in _sources():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                func = node.func
                called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                if called in ("__import__", "import_module"):
                    offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == []
