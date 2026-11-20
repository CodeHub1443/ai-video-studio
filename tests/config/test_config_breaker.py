"""I-8 / R-4: breaker thresholds live in configuration only; the code has no default numbers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from studio.config.settings import BreakerConfig, RecoveryConfig, StudioSettings

REAL_CONFIG = Path(__file__).resolve().parents[2] / "config"
BASE_BREAKER = "[recovery.breaker]\nk = 3\nwindow_s = 1800\n"


def make(**kwargs: Any) -> StudioSettings:
    return StudioSettings(**kwargs)


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "cfg"
    d.mkdir()
    monkeypatch.setenv("STUDIO_CONFIG_DIR", str(d))
    return d


# --- no code defaults ----------------------------------------------------------------------------------


def test_the_code_has_no_threshold_defaults():
    b = BreakerConfig()
    assert b.k is None and b.window_s is None
    assert b.provisional is True


def test_settings_without_configuration_expose_none_so_consumers_fail_closed(cfg: Path):
    b = make(env="dev").recovery.breaker
    assert (b.k, b.window_s, b.provisional) == (None, None, True)
    assert RecoveryConfig().breaker == BreakerConfig()


# --- the shipped configuration --------------------------------------------------------------------------


def test_the_shipped_studio_toml_sets_the_provisional_thresholds(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("STUDIO_CONFIG_DIR", str(REAL_CONFIG))
    b = make(env="dev").recovery.breaker
    assert (b.k, b.window_s, b.provisional) == (3, 1800, True)


def test_the_shipped_studio_toml_documents_the_values_as_provisional():
    text = (REAL_CONFIG / "studio.toml").read_text(encoding="utf-8")
    section = text[text.index("[recovery.breaker]") : text.index("[comfyui]")]
    assert "PROVISIONAL" in section and "NOT CALIBRATED" in section
    assert "provisional = true" in section


# --- validation ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("k", [1, 2, 3, 50])
def test_k_accepts_integers_from_one(k: int):
    assert BreakerConfig(k=k).k == k


@pytest.mark.parametrize("k", [0, -1, -100, 1.5, "three", True, False, [3]])
def test_k_rejects_everything_below_one_and_non_integers(k: Any):
    with pytest.raises(ValidationError):
        BreakerConfig(k=k)


@pytest.mark.parametrize("window_s", [1, 60, 1800, 86400])
def test_window_accepts_positive_integers(window_s: int):
    assert BreakerConfig(window_s=window_s).window_s == window_s


@pytest.mark.parametrize("window_s", [0, -1, -1800, 0.5, "soon", True, False])
def test_window_rejects_zero_negative_and_non_integers(window_s: Any):
    with pytest.raises(ValidationError):
        BreakerConfig(window_s=window_s)


def test_unknown_keys_are_rejected_and_the_section_is_frozen():
    with pytest.raises(ValidationError):
        BreakerConfig(window=30)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        RecoveryConfig(breaker={"k": 3}, extra=1)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        BreakerConfig(k=3).k = 4  # type: ignore[misc]


# --- how it is configured ------------------------------------------------------------------------------


def test_a_toml_file_can_set_a_partial_section(cfg: Path):
    (cfg / "studio.toml").write_text("[recovery.breaker]\nk = 2\n", encoding="utf-8")
    b = make(env="dev").recovery.breaker
    assert (b.k, b.window_s) == (2, None)


@pytest.mark.parametrize(
    "toml", ["k = 0", "window_s = 0", "window_s = -5", "k = true", "k = 'x'", "bogus = 1"]
)
def test_invalid_toml_values_fail_construction(cfg: Path, toml: str):
    (cfg / "studio.toml").write_text(f"[recovery.breaker]\n{toml}\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        make(env="dev")


def test_environment_variables_and_kwargs_override_the_file(
    cfg: Path, monkeypatch: pytest.MonkeyPatch
):
    (cfg / "studio.toml").write_text(
        "[recovery.breaker]\nk = 3\nwindow_s = 1800\n", encoding="utf-8"
    )
    monkeypatch.setenv("STUDIO_RECOVERY__BREAKER__K", "5")
    b = make(env="dev").recovery.breaker
    assert (b.k, b.window_s) == (5, 1800)
    b = make(env="dev", recovery={"breaker": {"window_s": 60}}).recovery.breaker
    assert (b.k, b.window_s) == (5, 60)
    monkeypatch.setenv("STUDIO_RECOVERY__BREAKER__K", "0")
    with pytest.raises(ValidationError):
        make(env="dev")


def test_env_test_inherits_the_breaker_from_the_base_file_without_its_own_section(
    cfg: Path, tmp_path: Path
):
    (cfg / "studio.toml").write_text(BASE_BREAKER, encoding="utf-8")
    (cfg / "studio.test.toml").write_text("[comfyui]\nport = 18188\n", encoding="utf-8")
    sb = tmp_path / "sandbox"
    s = make(
        env="test",
        test_sandbox_root=sb,
        paths={
            "workspace_root": sb / "ws",
            "ltx_repo": sb / "ltx",
            "ltx_models": sb / "ltx_models",
        },
        ffmpeg={"ffmpeg": str(sb / "ffmpeg.exe"), "ffprobe": str(sb / "ffprobe.exe")},
    )
    b = s.recovery.breaker
    assert (b.k, b.window_s, b.provisional) == (3, 1800, True)
