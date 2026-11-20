"""Typed application settings.

Construction is PURE with respect to the studio's data: building a settings object validates values
lexically, never creates directories, never touches the network and never imports an engine. (Reading the
small config TOML files is the one filesystem read it performs.) Existence checks live in ``checks.py``.

Source precedence (highest first): init kwargs, ``STUDIO_*`` environment variables (nested delimiter
``__``, e.g. ``STUDIO_PATHS__WORKSPACE_ROOT``), ``studio.<env>.toml``, ``studio.toml``, built-in defaults.

Environments (approved decision 5):
* ``dev``  - enabled.
* ``test`` - isolated: every path must lie inside an explicit ``test_sandbox_root``; no live engines.
* ``prod`` - DISABLED pending a separate approved design; constructing settings for it raises.

The environment comes from the ``env`` init kwarg if given, otherwise ``STUDIO_ENV`` (default ``dev``), is
validated strictly, and the SAME value selects the overlay file and the ``env`` field. A TOML file cannot
set ``env``. Output namespaces are pinned in ``studio.config.namespaces`` and are not configurable.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from enum import StrEnum
from functools import lru_cache
from pathlib import Path, PureWindowsPath
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from studio.config.hardware import validate_profile_name
from studio.config.namespaces import STUDIO_OUTPUTS_SUBDIR
from studio.config.paths import (
    PathValidationError,
    is_within,
    overlaps,
    same_path,
    validate_absolute_windows_path,
    validate_segment,
)

DEFAULT_WORKSPACE_ROOT = Path(r"E:\AI_VIDEO_STUDIO")
DEFAULT_LTX_REPO = Path(r"E:\Dev\ltx25-igpu-streaming")
DEFAULT_LTX_MODELS = Path(r"E:\AI-Data\LTX25")
PINNED_AI_DATA_ROOT = Path(
    r"E:\AI-Data"
)  # every model store lives below it; test sandboxes never may
DEFAULT_COMFYUI_PORT = 8188  # the real, user-run ComfyUI; env=test must never be pointed at it

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_LOG_LEVEL_ALIASES = {"WARN": "WARNING", "FATAL": "CRITICAL"}
PROD_DISABLED_MESSAGE = (
    "env=prod is disabled: production behaviour awaits a separate approved design "
    "(see docs/adr/0008). Use dev or test."
)


class Environment(StrEnum):
    DEV = "dev"
    TEST = "test"
    PROD = "prod"  # recognised so that it can be refused with a clear message


def parse_environment(raw: object) -> Environment:
    """Strict, case-insensitive parse of an environment name. Raises ``ValueError`` on anything else."""
    if isinstance(raw, Environment):
        return raw
    if not isinstance(raw, str):
        raise ValueError(f"environment must be a string, got {type(raw).__name__}")
    try:
        return Environment(raw.strip().lower())
    except ValueError:
        allowed = ", ".join(e.value for e in Environment)
        raise ValueError(f"invalid environment {raw!r}; expected one of: {allowed}") from None


def resolve_environment(init_kwargs: Mapping[str, Any] | None = None) -> Environment:
    """init kwarg ``env`` > ``STUDIO_ENV`` > dev. An empty ``STUDIO_ENV`` is an error, not 'unset'."""
    if init_kwargs and "env" in init_kwargs:
        return parse_environment(init_kwargs["env"])
    raw = os.environ.get("STUDIO_ENV")
    if raw is None:
        return Environment.DEV
    return parse_environment(raw)


class ConfigDirNotFound(FileNotFoundError):
    """No config directory could be located; set STUDIO_CONFIG_DIR."""


def _source_checkout_config_dir() -> Path | None:
    """``<software>/config`` when running from a source checkout; ``None`` when installed as a package.

    A wheel puts this module under site-packages, where ``parents[3]/config`` is meaningless, so the
    location is accepted only if it sits next to ``pyproject.toml`` and contains ``studio.toml``.
    """
    software = Path(__file__).resolve().parents[3]
    candidate = software / "config"
    if (software / "pyproject.toml").is_file() and (candidate / "studio.toml").is_file():
        return candidate
    return None


def config_dir() -> Path:
    """``STUDIO_CONFIG_DIR`` if set (must be absolute), else the source checkout's config folder.

    Raises ``ConfigDirNotFound`` for an installed package with no override.
    """
    override = os.environ.get("STUDIO_CONFIG_DIR")
    if override:
        validate_absolute_windows_path(override)
        return Path(override)
    found = _source_checkout_config_dir()
    if found is None:
        raise ConfigDirNotFound(
            "no config directory: STUDIO_CONFIG_DIR is not set and this is not a source checkout"
        )
    return found


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PathsConfig(_Section):
    """Explicit locations, in three classes (docs/security-model.md):

    * studio-writable roots: projects, cache, logs, data, and the pinned ``outputs\\studio`` subfolder;
    * ``assets_root``: read-only originals. The studio never writes, moves or deletes there;
    * external roots (models, ComfyUI, LTX): read-only to the studio. ComfyUI and LTX themselves write
      only to their pinned namespaces (``studio.config.namespaces``); the studio only reads those.
    """

    workspace_root: Path
    projects_root: Path
    assets_root: Path  # READ-ONLY ORIGINALS
    outputs_root: Path
    cache_root: Path
    logs_root: Path
    data_root: Path  # holds studio.db (ADR-0004)
    models_root: Path  # shared model files (also read by ComfyUI via extra_model_paths.yaml)
    comfyui_root: Path
    comfyui_output_dir: Path
    ltx_repo: Path
    ltx_python: Path
    ltx_models: Path

    @model_validator(mode="before")
    @classmethod
    def _derive_defaults(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        d = dict(data)
        ws = Path(d.setdefault("workspace_root", DEFAULT_WORKSPACE_ROOT))
        for key, rel in (
            ("projects_root", "projects"),
            ("assets_root", "assets"),
            ("outputs_root", "outputs"),
            ("cache_root", "cache"),
            ("logs_root", "logs"),
            ("data_root", "data"),
            ("models_root", "models"),
            ("comfyui_root", Path("comfyui") / "ComfyUI"),
        ):
            d.setdefault(key, ws / rel)
        d.setdefault("comfyui_output_dir", Path(d["comfyui_root"]) / "output")
        ltx = Path(d.setdefault("ltx_repo", DEFAULT_LTX_REPO))
        d.setdefault("ltx_python", ltx / ".venv" / "Scripts" / "python.exe")
        d.setdefault("ltx_models", DEFAULT_LTX_MODELS)
        return d

    @property
    def database_path(self) -> Path:
        return self.data_root / "studio.db"

    @property
    def software_root(self) -> Path:
        return self.workspace_root / "software"

    @property
    def studio_outputs_dir(self) -> Path:
        """The only location under ``outputs_root`` the studio may write (pinned name)."""
        return self.outputs_root / STUDIO_OUTPUTS_SUBDIR

    @property
    def studio_roots(self) -> dict[str, Path]:
        """Every studio-managed root, for layout validation. Includes read-only ``assets_root``."""
        return {
            "projects_root": self.projects_root,
            "assets_root": self.assets_root,
            "outputs_root": self.outputs_root,
            "cache_root": self.cache_root,
            "logs_root": self.logs_root,
            "data_root": self.data_root,
        }

    @property
    def read_only_originals(self) -> dict[str, Path]:
        return {"assets_root": self.assets_root}

    @property
    def writable_roots(self) -> dict[str, Path]:
        """Where the studio may write (later, under an authorized step). ``outputs_root`` is narrowed."""
        return {
            "projects_root": self.projects_root,
            "studio_outputs_dir": self.studio_outputs_dir,
            "cache_root": self.cache_root,
            "logs_root": self.logs_root,
            "data_root": self.data_root,
        }

    @property
    def external_roots(self) -> dict[str, Path]:
        return {
            "models_root": self.models_root,
            "comfyui_root": self.comfyui_root,
            "ltx_repo": self.ltx_repo,
            "ltx_models": self.ltx_models,
        }

    @property
    def all_paths(self) -> dict[str, Path]:
        return {
            "workspace_root": self.workspace_root,
            **self.studio_roots,
            **self.external_roots,
            "comfyui_output_dir": self.comfyui_output_dir,
            "ltx_python": self.ltx_python,
        }

    @model_validator(mode="after")
    def _validate_layout(self) -> Self:
        problems: list[str] = []
        owned, external = self.studio_roots, self.external_roots
        for name, p in self.all_paths.items():
            try:
                validate_absolute_windows_path(p)
            except PathValidationError as exc:
                problems.append(f"{name}: {exc}")
        if problems:  # the structural checks below assume well-formed absolute paths
            raise ValueError("; ".join(problems))
        for name, p in owned.items():
            if not is_within(p, self.workspace_root):
                problems.append(f"{name} must be inside workspace_root: {p}")
            if same_path(p, self.software_root) or is_within(p, self.software_root):
                problems.append(f"{name} must not be inside the software folder: {p}")
        names = list(owned)
        for i, a in enumerate(names):
            for b in names[i + 1 :]:
                if overlaps(owned[a], owned[b]):
                    problems.append(f"studio roots must not nest: {a} vs {b}")
            for ename, ep in external.items():
                if overlaps(owned[a], ep):
                    problems.append(f"{a} must not overlap external root {ename}: {owned[a]}")
        enames = list(external)
        for i, a in enumerate(enames):
            for b in enames[i + 1 :]:
                if overlaps(external[a], external[b]):
                    problems.append(f"external roots must not overlap: {a} vs {b}")
        if not is_within(self.comfyui_output_dir, self.comfyui_root):
            problems.append("comfyui_output_dir must be inside comfyui_root")
        # ltx_python may live outside ltx_repo (another venv); it may never live in studio-managed
        # roots, and must be python.exe. Being outside ltx_repo is only reported by checks.py.
        if self.ltx_python.name.lower() != "python.exe":
            problems.append(f"ltx_python must point to python.exe: {self.ltx_python}")
        for name, p in owned.items():
            if overlaps(self.ltx_python, p):
                problems.append(
                    f"ltx_python must not be inside studio root {name}: {self.ltx_python}"
                )
        if problems:
            raise ValueError("; ".join(problems))
        return self


class ComfyUIConfig(_Section):
    """Connection details for the externally managed ComfyUI (ADR-0002). The studio never starts it.
    The output namespace is pinned (``studio.config.namespaces``), not configured here."""

    host: str = "127.0.0.1"
    port: int = Field(default=DEFAULT_COMFYUI_PORT, ge=1, le=65535)
    allow_remote: bool = False
    client_id_prefix: str = Field(default="studio", pattern=r"^[a-z0-9_-]+$")
    request_timeout_s: float = Field(default=30.0, gt=0)

    @model_validator(mode="after")
    def _loopback_only(self) -> Self:
        if not self.allow_remote and self.host.lower() not in _LOOPBACK_HOSTS:
            raise ValueError(
                f"ComfyUI host {self.host!r} is not loopback; set allow_remote=true to override"
            )
        return self

    @property
    def base_url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"

    @property
    def ws_url(self) -> str:
        return "ws://" + self.base_url.removeprefix("http://") + "/ws"


class LtxConfig(_Section):
    """How the unchanged LTX CLI is addressed (ADR-0003). Paths live in PathsConfig; the output
    category and name prefix are pinned (``studio.config.namespaces``), not configured here."""

    render_script: str = "render.py"

    @field_validator("render_script")
    @classmethod
    def _script_segment(cls, v: str) -> str:
        try:
            return validate_segment(v)
        except PathValidationError as exc:
            raise ValueError(f"render_script: {exc}") from None


class FfmpegConfig(_Section):
    """Executable names or absolute paths. Resolved against PATH later by the doctor, not here.
    In env=test they must be absolute paths inside the sandbox (never the real tools)."""

    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"


class _OverlayToml(TomlConfigSettingsSource):
    """TOML source that refuses ``env`` so a file can never contradict the selected environment."""

    def __call__(self) -> dict[str, Any]:
        data = super().__call__()
        if "env" in data:
            raise ValueError(
                "'env' cannot be set in a TOML file; use STUDIO_ENV or the env= argument"
            )
        return data


def _init_kwargs(source: PydanticBaseSettingsSource) -> Mapping[str, Any]:
    """The init kwargs of this construction. Relies on pydantic-settings internals (open item U-9)."""
    data = getattr(source, "init_kwargs", None)
    if isinstance(data, Mapping):
        return data
    try:
        data = source()
    except Exception:  # noqa: BLE001 - fall back to STUDIO_ENV
        return {}
    return data if isinstance(data, Mapping) else {}


class StudioSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="STUDIO_",
        env_nested_delimiter="__",
        extra="forbid",
        frozen=True,
    )

    env: Environment = Environment.DEV
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    hardware_profile: str = "rtx3050-6gb"
    registry_dir: Path | None = None  # default: <config_dir>/registry
    # Required (and only allowed) when env=test: the explicit allowlisted sandbox every path must be in.
    test_sandbox_root: Path | None = None
    paths: PathsConfig = Field(default_factory=lambda: PathsConfig.model_validate({}))
    comfyui: ComfyUIConfig = Field(default_factory=ComfyUIConfig)
    ltx: LtxConfig = Field(default_factory=LtxConfig)
    ffmpeg: FfmpegConfig = Field(default_factory=FfmpegConfig)

    @field_validator("env", mode="before")
    @classmethod
    def _parse_env(cls, v: object) -> Environment:
        env = parse_environment(v)
        if env is Environment.PROD:
            raise ValueError(PROD_DISABLED_MESSAGE)
        return env

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalize_log_level(cls, v: object) -> object:
        if isinstance(v, str):
            level = v.strip().upper()
            return _LOG_LEVEL_ALIASES.get(level, level)
        return v

    @field_validator("hardware_profile")
    @classmethod
    def _profile_name(cls, v: str) -> str:
        return validate_profile_name(v)

    @field_validator("registry_dir", "test_sandbox_root")
    @classmethod
    def _absolute(cls, v: Path | None) -> Path | None:
        if v is not None:
            try:
                validate_absolute_windows_path(v)
            except PathValidationError as exc:
                raise ValueError(str(exc)) from None
        return v

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Same resolution as the ``env`` field: an ``env=`` kwarg selects the overlay too.
        env_name = resolve_environment(
            _init_kwargs(init_settings)
        ).value  # validated file-name part
        try:
            cfg: Path | None = config_dir()
        except ConfigDirNotFound:
            cfg = None  # installed package without STUDIO_CONFIG_DIR: built-in defaults only
        files = [] if cfg is None else [cfg / "studio.toml", cfg / f"studio.{env_name}.toml"]
        toml = _OverlayToml(
            settings_cls, toml_file=files
        )  # later file wins; missing files are skipped
        return (init_settings, env_settings, toml)

    @model_validator(mode="after")
    def _environment_rules(self) -> Self:
        problems: list[str] = []
        if self.env is Environment.TEST:
            problems = _test_isolation_problems(self)
        elif self.test_sandbox_root is not None:
            problems = ["test_sandbox_root is only valid when env=test"]
        if problems:
            raise ValueError(f"env={self.env.value}: " + "; ".join(problems))
        return self

    @property
    def allows_live_engines(self) -> bool:
        """False in ``test``: adapters (M2+) must refuse to contact ComfyUI/LTX/NVML when this is False."""
        return self.env is not Environment.TEST

    @property
    def config_dir(self) -> Path:
        return config_dir()

    @property
    def effective_registry_dir(self) -> Path:
        return self.registry_dir if self.registry_dir is not None else config_dir() / "registry"

    @property
    def access_policy(self) -> Any:
        """``studio.config.scopes.AccessPolicy`` for these settings (imported lazily: no import cycle)."""
        from studio.config.scopes import build_access_policy

        return build_access_policy(self.paths)


def _base_config_roots() -> tuple[dict[str, Path], str | None]:
    """Every path of the BASE configuration (``studio.toml`` alone), plus a problem text if it is unusable.

    No overlay, no ``STUDIO_*`` variable and no init kwarg can influence this: a test must not be able to
    shrink the set of real locations it is protected from. No config directory or no ``studio.toml`` means
    the built-in defaults apply (already protected). A file that exists but cannot be read, parsed or
    validated fails closed.
    """
    try:
        base = config_dir() / "studio.toml"
    except ConfigDirNotFound:
        return {}, None
    except Exception as exc:  # noqa: BLE001 - fail closed on any configuration-directory problem
        return {}, f"cannot locate the base configuration to protect its real roots: {exc}"
    try:
        text = base.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, None
    except (OSError, ValueError) as exc:
        return {}, f"cannot read the base configuration {base} to protect its real roots: {exc}"
    try:
        table = tomllib.loads(text).get("paths", {})
        if not isinstance(table, dict):
            raise ValueError("'paths' must be a table")
        paths = PathsConfig.model_validate(table)
    except ValueError as exc:  # TOMLDecodeError and pydantic ValidationError are ValueErrors
        return {}, f"invalid base configuration {base}; cannot protect its real roots: {exc}"
    return dict(paths.all_paths), None


def _protected_real_roots() -> tuple[dict[str, Path], str | None]:
    """Locations an env=test sandbox may never overlap: the built-in real locations, the pinned
    AI-Data root (drive E) and every path of the base configuration. The second item is a fail-closed
    problem text."""
    protected: dict[str, Path] = {
        "real workspace": DEFAULT_WORKSPACE_ROOT,
        "protected LTX repository": DEFAULT_LTX_REPO,
        "real LTX weights": DEFAULT_LTX_MODELS,
        "pinned AI-Data root": PINNED_AI_DATA_ROOT,
    }
    base, problem = _base_config_roots()
    for name, path in base.items():
        protected[f"base-configured paths.{name}"] = path
    return protected, problem


def _test_isolation_problems(s: StudioSettings) -> list[str]:
    """env=test: every location must be inside the explicit sandbox allowlist.

    Lexical, so a link inside the sandbox is not detected here; ``guard.authorize`` and
    ``check_environment`` cover that. Nothing proves the sandbox is a temporary directory (open item U-6).
    """
    sandbox = s.test_sandbox_root
    if sandbox is None:
        return ["test_sandbox_root is required (explicit allowlisted sandbox for all test paths)"]
    problems: list[str] = []
    if len(PureWindowsPath(str(sandbox)).parts) < 3:
        problems.append(
            f"test_sandbox_root must be at least two levels below the drive root: {sandbox}"
        )
    protected, protect_problem = _protected_real_roots()
    if protect_problem is not None:
        problems.append(protect_problem)
    for label, real in protected.items():
        if overlaps(sandbox, real):
            problems.append(f"test_sandbox_root overlaps the {label}")
    for pname, p in s.paths.all_paths.items():
        if not is_within(p, sandbox):
            problems.append(f"paths.{pname} must be inside test_sandbox_root: {p}")
    for tool in ("ffmpeg", "ffprobe"):
        value = getattr(s.ffmpeg, tool)
        if not PureWindowsPath(value).drive:
            problems.append(
                f"ffmpeg.{tool} must be an absolute path inside the sandbox (a bare name resolves "
                "to the real tool via PATH)"
            )
        elif not is_within(value, sandbox):
            problems.append(f"ffmpeg.{tool} must be inside test_sandbox_root: {value}")
    if s.comfyui.port == DEFAULT_COMFYUI_PORT:
        problems.append(f"comfyui.port must not be the real ComfyUI port {DEFAULT_COMFYUI_PORT}")
    if s.comfyui.allow_remote:
        problems.append("comfyui.allow_remote must be false")
    return problems


@lru_cache(maxsize=1)
def get_settings() -> StudioSettings:
    """Cached settings for application code. Tests should construct ``StudioSettings`` directly.

    The cache does not notice later changes to environment variables; call ``get_settings.cache_clear()``.
    """
    return StudioSettings()
