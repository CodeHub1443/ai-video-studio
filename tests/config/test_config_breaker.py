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


@pytest.mark.parametrize("k", [0, -1, -100, 1.5, 3.0, 1.0, "three", "3", True, False, [3]])
def test_k_rejects_everything_below_one_and_non_integers(k: Any):
    with pytest.raises(ValidationError):
        BreakerConfig(k=k)


@pytest.mark.parametrize("window_s", [1, 60, 1800, 86400])
def test_window_accepts_positive_integers(window_s: int):
    assert BreakerConfig(window_s=window_s).window_s == window_s


@pytest.mark.parametrize("window_s", [0, -1, -1800, 0.5, 1800.0, "soon", "1800", True, False])
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
    "toml",
    [
        "k = 0",
        "window_s = 0",
        "window_s = -5",
        "k = true",
        "k = 'x'",
        "k = '3'",
        "k = 3.0",
        "window_s = '1800'",
        "window_s = 1800.0",
        "bogus = 1",
    ],
)
def test_invalid_toml_values_fail_construction(cfg: Path, toml: str):
    (cfg / "studio.toml").write_text(f"[recovery.breaker]\n{toml}\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        make(env="dev")


def test_the_base_file_values_load(cfg: Path):
    (cfg / "studio.toml").write_text(BASE_BREAKER, encoding="utf-8")
    b = make(env="dev").recovery.breaker
    assert (b.k, b.window_s, b.provisional) == (3, 1800, True)


def test_constructor_kwargs_are_allowed_and_merge_over_the_base_file(cfg: Path):
    (cfg / "studio.toml").write_text(BASE_BREAKER, encoding="utf-8")
    b = make(env="dev", recovery={"breaker": {"window_s": 60}}).recovery.breaker
    assert (b.k, b.window_s) == (3, 60)
    b = make(env="dev", recovery={"breaker": {"k": 5, "window_s": 120}}).recovery.breaker
    assert (b.k, b.window_s) == (5, 120)


@pytest.mark.parametrize("bad", [{"k": 3.0}, {"k": "3"}, {"k": True}, {"window_s": 1800.0}])
def test_constructor_kwargs_must_be_strict_integers(cfg: Path, bad: dict[str, Any]):
    with pytest.raises(ValidationError):
        make(env="dev", recovery={"breaker": bad})


# --- R-8: environment variables and overlays may not set the breaker ----------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "STUDIO_RECOVERY__BREAKER__K",
        "STUDIO_RECOVERY__BREAKER__WINDOW_S",
        "STUDIO_RECOVERY__BREAKER__PROVISIONAL",
        "STUDIO_RECOVERY__BREAKER",
        "STUDIO_RECOVERY",
    ],
)
def test_r8_a_recovery_environment_variable_is_a_settings_error(
    cfg: Path, monkeypatch: pytest.MonkeyPatch, name: str
):
    (cfg / "studio.toml").write_text(BASE_BREAKER, encoding="utf-8")
    monkeypatch.setenv(name, "99917")
    with pytest.raises(ValueError, match="base studio.toml .ruling R-8.") as info:
        make(env="dev")
    assert name in str(info.value)
    assert "99917" not in str(info.value)  # names only, never values


def test_r8_the_json_form_cannot_weaken_the_breaker(cfg: Path, monkeypatch: pytest.MonkeyPatch):
    (cfg / "studio.toml").write_text(BASE_BREAKER, encoding="utf-8")
    monkeypatch.setenv("STUDIO_RECOVERY", '{"breaker": {"k": 1000, "window_s": 1}}')
    with pytest.raises(ValueError, match="R-8"):
        make(env="dev")


def test_r8_the_environment_rule_applies_to_env_test_too(
    cfg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("STUDIO_RECOVERY__BREAKER__K", "2")
    sb = tmp_path / "sandbox"
    with pytest.raises(ValueError, match="R-8"):
        make(env="test", test_sandbox_root=sb)


def test_r8_other_studio_variables_are_unaffected(cfg: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("STUDIO_COMFYUI__PORT", "19004")
    monkeypatch.setenv("STUDIO_RECOVERYX", "1")  # not the recovery prefix
    assert make(env="dev").comfyui.port == 19004


@pytest.mark.parametrize(
    "overlay", ["[recovery.breaker]\nk = 1000\n", "[recovery]\n", "[recovery.breaker]\n"]
)
def test_r8_a_recovery_table_in_the_selected_overlay_is_a_settings_error(cfg: Path, overlay: str):
    (cfg / "studio.toml").write_text(BASE_BREAKER, encoding="utf-8")
    (cfg / "studio.dev.toml").write_text(overlay, encoding="utf-8")
    with pytest.raises(ValueError, match=r"R-8.*overlay file studio\.dev\.toml"):
        make(env="dev")


def test_r8_the_test_overlay_is_refused_too(cfg: Path, tmp_path: Path):
    (cfg / "studio.test.toml").write_text("[recovery.breaker]\nk = 2\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"R-8.*overlay file studio\.test\.toml"):
        make(env="test", test_sandbox_root=tmp_path / "sandbox")


def test_r8_an_overlay_of_another_environment_is_not_read(cfg: Path):
    (cfg / "studio.toml").write_text(BASE_BREAKER, encoding="utf-8")
    (cfg / "studio.test.toml").write_text("[recovery.breaker]\nk = 2\n", encoding="utf-8")
    assert make(env="dev").recovery.breaker.k == 3


def test_r8_an_overlay_without_a_recovery_table_still_works(cfg: Path):
    (cfg / "studio.toml").write_text(BASE_BREAKER, encoding="utf-8")
    (cfg / "studio.dev.toml").write_text("[comfyui]\nport = 19005\n", encoding="utf-8")
    s = make(env="dev")
    assert s.comfyui.port == 19005 and s.recovery.breaker.k == 3


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
