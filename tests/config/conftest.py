"""Config tests must not see the developer's environment."""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _clean_studio_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("STUDIO_"):
            monkeypatch.delenv(name)
