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
    monkeypatch.setattr(settings_module, "_checkout_base_file", lambda: None)
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


# --- D-1: the checkout's configured roots are protected whatever STUDIO_CONFIG_DIR says ---------------

REAL_CONFIG = Path(__file__).resolve().parents[2] / "config"


@pytest.fixture
def checkout_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A stand-in for the source checkout's config/studio.toml that configures D:\\Real\\WS."""
    f = write(tmp_path / "checkout" / "studio.toml", "[paths]\nworkspace_root = 'D:\\Real\\WS'\n")
    monkeypatch.setattr(settings_module, "_checkout_base_file", lambda: f)
    return f


def test_d1_an_empty_config_dir_override_no_longer_unprotects_the_checkout_roots(
    cfg: Path, checkout_file: Path
):
    """B-12 probe B: STUDIO_CONFIG_DIR points at an empty directory, the checkout configures D:\\Real\\WS."""
    assert not (cfg / "studio.toml").exists()
    with pytest.raises(
        ValidationError, match="overlaps the checkout-configured paths.workspace_root"
    ):
        make(**test_kwargs(Path(r"D:\Real\WS\sandbox")))


def test_d1_both_the_checkout_file_and_the_override_file_are_protected(
    cfg: Path, checkout_file: Path
):
    write(cfg / "studio.toml", "[paths]\nltx_repo = 'D:\\Other\\LTX'\n")
    with pytest.raises(
        ValidationError, match="overlaps the checkout-configured paths.workspace_root"
    ):
        make(**test_kwargs(Path(r"D:\Real\WS\sandbox")))
    with pytest.raises(ValidationError, match="overlaps the base-configured paths.ltx_repo"):
        make(**test_kwargs(Path(r"D:\Other\LTX\sandbox")))


def test_d1_the_same_file_is_not_read_twice_or_relabelled(
    cfg: Path, monkeypatch: pytest.MonkeyPatch
):
    same = write(cfg / "studio.toml", "[paths]\nworkspace_root = 'D:\\Real\\WS'\n")
    monkeypatch.setattr(settings_module, "_checkout_base_file", lambda: same)
    protected, problem = settings_module._protected_real_roots()  # pyright: ignore[reportPrivateUsage]
    assert problem is None
    assert "checkout-configured paths.workspace_root" in protected
    assert not [k for k in protected if k.startswith("base-configured")]


def test_d1_a_sandbox_elsewhere_is_still_accepted(cfg: Path, checkout_file: Path, sandbox: Path):
    assert make(**test_kwargs(sandbox)).env.value == "test"


def test_d1_the_real_checkout_file_is_located_without_the_override(cfg: Path):
    located = settings_module._checkout_base_file()  # pyright: ignore[reportPrivateUsage]
    assert located == REAL_CONFIG / "studio.toml"
    protected, problem = settings_module._protected_real_roots()  # pyright: ignore[reportPrivateUsage]
    assert problem is None
    assert protected["checkout-configured paths.workspace_root"] == Path(r"E:\AI_VIDEO_STUDIO")


def test_d1_a_tree_without_pyproject_is_not_a_checkout(
    cfg: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    fake_module = tmp_path / "site" / "a" / "b" / "settings.py"
    fake_module.parent.mkdir(parents=True)
    monkeypatch.setattr(settings_module, "__file__", str(fake_module))
    assert settings_module._checkout_base_file() is None  # pyright: ignore[reportPrivateUsage]


def test_d1_a_missing_checkout_file_contributes_nothing(
    cfg: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setattr(
        settings_module, "_checkout_base_file", lambda: tmp_path / "nowhere" / "studio.toml"
    )
    protected, problem = settings_module._protected_real_roots()  # pyright: ignore[reportPrivateUsage]
    assert problem is None
    assert set(protected) == {
        "real workspace",
        "protected LTX repository",
        "real LTX weights",
        "pinned AI-Data root",
    }


# --- fail closed: SET-1's own path, reached directly -------------------------------------------------

BAD_BASE_FILES = {
    "bad-toml": "this is = not [toml",
    "unknown-key": "[paths]\nunknown_key = 'D:\\x'\n",
    "relative-path": "[paths]\nworkspace_root = 'relative\\path'\n",
    "paths-not-a-table": "paths = 3\n",
}


def _valid_test_settings(sandbox: Path) -> StudioSettings:
    return make(**test_kwargs(sandbox))


@pytest.mark.parametrize("content", list(BAD_BASE_FILES.values()), ids=list(BAD_BASE_FILES))
def test_set1_reports_a_problem_for_each_kind_of_broken_override_file(
    cfg: Path, sandbox: Path, content: str
):
    s = _valid_test_settings(sandbox)  # built while the file is still valid
    write(cfg / "studio.toml", content)
    problems = settings_module._test_isolation_problems(s)  # pyright: ignore[reportPrivateUsage]
    assert any("invalid base configuration" in p and "studio.toml" in p for p in problems), problems


@pytest.mark.parametrize("content", list(BAD_BASE_FILES.values()), ids=list(BAD_BASE_FILES))
def test_set1_reports_a_problem_for_each_kind_of_broken_checkout_file(
    cfg: Path, sandbox: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str
):
    s = _valid_test_settings(sandbox)
    broken = write(tmp_path / "checkout" / "studio.toml", content)
    monkeypatch.setattr(settings_module, "_checkout_base_file", lambda: broken)
    problems = settings_module._test_isolation_problems(s)  # pyright: ignore[reportPrivateUsage]
    assert any("invalid base configuration" in p and "checkout" in p for p in problems), problems


@pytest.mark.parametrize("which", ["override", "checkout"])
def test_set1_reports_a_problem_for_an_unreadable_base_file(
    cfg: Path, sandbox: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, which: str
):
    s = _valid_test_settings(sandbox)
    if which == "override":
        (cfg / "studio.toml").mkdir()  # a directory where the file should be
    else:
        unreadable = tmp_path / "checkout" / "studio.toml"
        unreadable.mkdir(parents=True)
        monkeypatch.setattr(settings_module, "_checkout_base_file", lambda: unreadable)
    problems = settings_module._test_isolation_problems(s)  # pyright: ignore[reportPrivateUsage]
    assert any("cannot read the base configuration" in p for p in problems), problems


def test_set1_reports_a_problem_when_the_config_dir_override_is_not_usable(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch, cfg: Path
):
    s = _valid_test_settings(sandbox)
    monkeypatch.setenv("STUDIO_CONFIG_DIR", "relative\\dir")
    problems = settings_module._test_isolation_problems(s)  # pyright: ignore[reportPrivateUsage]
    assert any("cannot locate the base configuration" in p for p in problems), problems


def test_set1_a_problem_in_either_file_is_reported_together(
    cfg: Path, sandbox: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    s = _valid_test_settings(sandbox)
    broken = write(tmp_path / "checkout" / "studio.toml", "paths = 3\n")
    monkeypatch.setattr(settings_module, "_checkout_base_file", lambda: broken)
    write(cfg / "studio.toml", "[paths]\nunknown_key = 'D:\\x'\n")
    (problem,) = [
        p
        for p in settings_module._test_isolation_problems(s)  # pyright: ignore[reportPrivateUsage]
        if "invalid base configuration" in p
    ]
    assert str(broken) in problem and str(cfg / "studio.toml") in problem
