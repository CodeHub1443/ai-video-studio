"""SET-1: env=test must protect the configured real roots, and the pydantic-settings behaviour it rests on.

Only ``tmp_path`` is written. Windows paths such as ``D:\\Real`` are used lexically; they are never touched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from studio.config import settings as settings_module
from studio.config.settings import (
    PROD_DISABLED_MESSAGE,
    StudioSettings,
)


def make(**kwargs: Any) -> StudioSettings:
    """Construct settings from loosely typed kwargs (pydantic-settings accepts raw values)."""
    return StudioSettings(**kwargs)


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty config directory selected through STUDIO_CONFIG_DIR."""
    d = tmp_path / "cfg"
    d.mkdir()
    monkeypatch.setenv("STUDIO_CONFIG_DIR", str(d))
    return d


def test_kwargs(sandbox: Path, **over: Any) -> dict[str, Any]:
    """A fully isolated env=test construction inside ``sandbox``."""
    kwargs: dict[str, Any] = {
        "env": "test",
        "test_sandbox_root": sandbox,
        "paths": {
            "workspace_root": sandbox / "ws",
            "ltx_repo": sandbox / "ltx",
            "ltx_models": sandbox / "ltx_models",
        },
        "comfyui": {"port": 18188},
        "ffmpeg": {
            "ffmpeg": str(sandbox / "bin" / "ffmpeg.exe"),
            "ffprobe": str(sandbox / "bin" / "ffprobe.exe"),
        },
    }
    kwargs.update(over)
    return kwargs


test_kwargs.__test__ = False  # type: ignore[attr-defined]  # a helper, not a test


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    return tmp_path / "sandbox"


# --- baseline: a clean sandbox is accepted -------------------------------------------------------


def test_clean_sandbox_without_base_paths_is_accepted(cfg: Path, sandbox: Path):
    s = make(**test_kwargs(sandbox))
    assert s.env.value == "test"
    assert s.allows_live_engines is False


def test_without_a_config_directory_the_defaults_are_protected_and_a_sandbox_is_accepted(
    monkeypatch: pytest.MonkeyPatch, sandbox: Path
):
    monkeypatch.delenv("STUDIO_CONFIG_DIR", raising=False)
    monkeypatch.setattr(settings_module, "_source_checkout_config_dir", lambda: None)
    assert make(**test_kwargs(sandbox)).env.value == "test"


# --- the built-in real locations and the pinned E:\AI-Data -------------------------------------------


@pytest.mark.parametrize(
    "bad, label",
    [
        (r"E:\AI_VIDEO_STUDIO\sandbox", "real workspace"),
        (r"E:\AI_VIDEO_STUDIO", "real workspace"),
        (r"E:\AI_VIDEO_STUDIO\models\sb", "real workspace"),
        (r"E:\Dev\ltx25-igpu-streaming\sandbox", "protected LTX repository"),
        (r"E:\AI-Data\LTX25\sandbox", "real LTX weights"),
        (r"E:\AI-Data\sandbox", "pinned AI-Data root"),
        (r"E:\AI-Data\Other\sandbox", "pinned AI-Data root"),
    ],
)
def test_sandbox_overlapping_a_built_in_real_root_is_rejected(cfg: Path, bad: str, label: str):
    sandbox = Path(bad)
    with pytest.raises(ValidationError, match=f"test_sandbox_root overlaps the {label}"):
        make(**test_kwargs(sandbox))


def test_sandbox_that_contains_a_real_root_is_rejected(cfg: Path):
    with pytest.raises(ValidationError, match="overlaps"):
        make(**test_kwargs(Path(r"E:\Dev")))


# --- SET-1: roots configured in the BASE file only ----------------------------------------------------


@pytest.fixture
def base_cfg(cfg: Path) -> Path:
    write(
        cfg / "studio.toml",
        "[paths]\n"
        "workspace_root = 'D:\\Real\\WS'\n"
        "ltx_repo = 'D:\\Real\\LTX'\n"
        "ltx_models = 'D:\\Real\\Weights'\n",
    )
    return cfg


@pytest.mark.parametrize(
    "bad, key",
    [
        (r"D:\Real\WS\sandbox", "workspace_root"),
        (r"D:\Real\WS\models\sb", "models_root"),
        (r"D:\Real\LTX\sandbox", "ltx_repo"),
        (r"D:\Real\Weights\sandbox", "ltx_models"),
        (r"D:\Real\WS", "workspace_root"),
    ],
)
def test_set1_sandbox_overlapping_a_base_configured_root_is_rejected(
    base_cfg: Path, bad: str, key: str
):
    with pytest.raises(ValidationError, match=f"overlaps the base-configured paths.{key}"):
        make(**test_kwargs(Path(bad)))


def test_set1_sandbox_elsewhere_is_accepted_with_a_base_config(base_cfg: Path, sandbox: Path):
    assert make(**test_kwargs(sandbox)).env.value == "test"


def test_set1_environment_variables_and_kwargs_cannot_shrink_the_protected_set(
    base_cfg: Path, monkeypatch: pytest.MonkeyPatch
):
    sandbox = Path(r"D:\Real\WS\sandbox")
    monkeypatch.setenv("STUDIO_PATHS__WORKSPACE_ROOT", str(sandbox / "ws"))
    monkeypatch.setenv("STUDIO_PATHS__LTX_REPO", str(sandbox / "ltx"))
    # kwargs move every effective path into the sandbox, so only the base file still protects D:\Real\WS
    with pytest.raises(ValidationError, match="overlaps the base-configured paths.workspace_root"):
        make(**test_kwargs(sandbox))


def test_set1_the_test_overlay_is_not_part_of_the_protected_set(cfg: Path, sandbox: Path):
    write(cfg / "studio.test.toml", "[paths]\nworkspace_root = 'D:\\Overlay'\n")
    write(cfg / "studio.toml", "")
    # accepted: only the base file counts (design E.3), and the effective paths come from kwargs
    assert make(**test_kwargs(sandbox)).env.value == "test"
    with pytest.raises(ValidationError):
        # ...but the overlay's path is an effective path and must itself be inside the sandbox
        make(
            env="test",
            test_sandbox_root=sandbox,
            comfyui={"port": 18188},
            ffmpeg={
                "ffmpeg": str(sandbox / "bin" / "ffmpeg.exe"),
                "ffprobe": str(sandbox / "bin" / "ffprobe.exe"),
            },
        )


# --- fail closed ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        "this is = not [toml",
        "[paths]\nunknown_key = 'D:\\x'\n",
        "[paths]\nworkspace_root = 'relative\\path'\n",
        "paths = 3\n",
    ],
    ids=["bad-toml", "unknown-key", "relative-path", "paths-not-a-table"],
)
def test_set1_an_invalid_base_file_fails_closed(cfg: Path, sandbox: Path, content: str):
    write(cfg / "studio.toml", content)
    with pytest.raises((ValueError, OSError)):  # the TOML source or the validators refuse it
        make(**test_kwargs(sandbox))


def test_set1_an_unreadable_base_file_fails_closed(cfg: Path, sandbox: Path):
    (cfg / "studio.toml").mkdir()  # a directory where the file should be: reading raises OSError
    with pytest.raises((ValueError, OSError)):
        make(**test_kwargs(sandbox))


def test_set1_a_base_configuration_problem_fails_test_construction(
    cfg: Path, sandbox: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        settings_module, "_base_config_roots", lambda: ({}, "boom: base configuration unusable")
    )
    with pytest.raises(ValidationError, match="boom: base configuration unusable"):
        make(**test_kwargs(sandbox))


def test_set1_protected_roots_function_reports_a_problem_for_a_broken_base(
    cfg: Path, monkeypatch: pytest.MonkeyPatch
):
    write(cfg / "studio.toml", "[paths]\nunknown_key = 'D:\\x'\n")
    protected, problem = settings_module._protected_real_roots()  # pyright: ignore[reportPrivateUsage]
    assert problem is not None and "invalid base configuration" in problem
    assert "pinned AI-Data root" in protected  # the built-ins are still reported


def test_set1_protected_roots_function_reads_only_the_base_file(cfg: Path):
    write(cfg / "studio.toml", "[paths]\nworkspace_root = 'D:\\Real\\WS'\n")
    write(cfg / "studio.test.toml", "[paths]\nworkspace_root = 'D:\\Overlay'\n")
    protected, problem = settings_module._protected_real_roots()  # pyright: ignore[reportPrivateUsage]
    assert problem is None
    assert protected["base-configured paths.workspace_root"] == Path(r"D:\Real\WS")
    assert Path(r"D:\Overlay") not in protected.values()
    assert Path(r"E:\AI-Data") in protected.values()
    assert Path(r"E:\AI_VIDEO_STUDIO") in protected.values()


# --- the existing rules still hold -------------------------------------------------------------------


def test_sandbox_is_required_for_test_and_forbidden_otherwise(cfg: Path, sandbox: Path):
    with pytest.raises(ValidationError, match="test_sandbox_root is required"):
        make(env="test")
    with pytest.raises(ValidationError, match="only valid when env=test"):
        make(env="dev", test_sandbox_root=sandbox)


def test_paths_outside_the_sandbox_and_the_real_comfyui_port_are_rejected(cfg: Path, sandbox: Path):
    kw = test_kwargs(sandbox)
    kw["paths"] = {"workspace_root": sandbox / "ws"}  # LTX locations fall back to the real ones
    with pytest.raises(ValidationError, match="must be inside test_sandbox_root"):
        make(**kw)
    with pytest.raises(ValidationError, match="real ComfyUI port"):
        make(**test_kwargs(sandbox, comfyui={"port": 8188}))
    with pytest.raises(ValidationError, match="bare name"):
        make(**test_kwargs(sandbox, ffmpeg={"ffmpeg": "ffmpeg", "ffprobe": "ffprobe"}))


# --- pydantic-settings behaviour (U-9, now testable) ---------------------------------------------------


def test_env_kwarg_selects_the_overlay_file(cfg: Path, sandbox: Path):
    write(cfg / "studio.test.toml", "[comfyui]\nport = 18999\n")
    kw = test_kwargs(sandbox)
    del kw["comfyui"]
    assert make(**kw).comfyui.port == 18999


def test_studio_env_variable_selects_the_overlay_and_the_environment(
    cfg: Path, sandbox: Path, monkeypatch: pytest.MonkeyPatch
):
    write(cfg / "studio.test.toml", "[comfyui]\nport = 18998\n")
    monkeypatch.setenv("STUDIO_ENV", "test")
    kw = test_kwargs(sandbox)
    del kw["env"], kw["comfyui"]
    s = make(**kw)
    assert s.env.value == "test" and s.comfyui.port == 18998


def test_dev_does_not_load_the_test_overlay(cfg: Path):
    write(cfg / "studio.test.toml", "[comfyui]\nport = 18997\n")
    assert make(env="dev").comfyui.port == 8188


def test_env_cannot_be_set_in_a_toml_file(cfg: Path):
    write(cfg / "studio.toml", 'env = "dev"\n')
    with pytest.raises(ValueError, match="cannot be set in a TOML file"):
        make(env="dev")


def test_prod_and_unknown_environments_are_refused(cfg: Path):
    with pytest.raises(ValidationError, match="env=prod is disabled"):
        make(env="prod")
    assert "prod" in PROD_DISABLED_MESSAGE
    with pytest.raises(
        ValueError, match="invalid environment"
    ):  # raised while choosing the overlay
        make(env="staging")


def test_nested_environment_variable_and_precedence(cfg: Path, monkeypatch: pytest.MonkeyPatch):
    write(cfg / "studio.toml", "[comfyui]\nport = 19001\n")
    assert make(env="dev").comfyui.port == 19001  # file
    monkeypatch.setenv("STUDIO_COMFYUI__PORT", "19002")
    assert make(env="dev").comfyui.port == 19002  # environment beats the file
    assert make(env="dev", comfyui={"port": 19003}).comfyui.port == 19003  # kwarg wins


def test_unknown_top_level_and_section_keys_are_rejected(cfg: Path):
    with pytest.raises(ValidationError):
        make(env="dev", surprise=1)  # type: ignore[call-arg]
    write(cfg / "studio.toml", "[comfyui]\nsurprise = 1\n")
    with pytest.raises(ValidationError):
        make(env="dev")
