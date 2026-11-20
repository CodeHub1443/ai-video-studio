# Configuration

Code: `src/studio/config/`. Data: `config/studio.toml`, `config/studio.<env>.toml`,
`config/hardware/<profile>.toml`. Security rationale: `docs/security-model.md`. Approval status of each
design point: `docs/adr/0008-configuration-and-registry-design.md` (Proposed).

## Principles
- **Constructing settings does not touch studio data.** It validates values lexically. It never stats a
  studio path, creates a directory, opens a socket or imports an engine. (It does read the small config
  TOML files.)
- **Checking the environment is separate** (`check_environment`): read-only stats, returns issues.
- **Three classes of location.** Studio-writable roots; `assets_root` as read-only originals; external
  roots (ComfyUI, LTX, models) that the studio only reads. ComfyUI and LTX write only to namespaces that are
  **pinned in code** (`studio.config.namespaces`), not configurable.

## Where the config folder is found
1. `STUDIO_CONFIG_DIR` (absolute path), else
2. `<software>/config` **only in a source checkout** (next to `pyproject.toml`, containing `studio.toml`), else
3. none: settings use built-in defaults and `config_dir()` raises `ConfigDirNotFound`. An installed package
   must set `STUDIO_CONFIG_DIR` (the wheel does not ship `config/`).

## Environments
Selection: `env=` argument, then `STUDIO_ENV`, then `dev`. Case-insensitive; anything else, including empty,
is an error. The value selects the overlay file **and** the `env` field. `env` cannot be set in a TOML file.

| | dev | test | prod |
|---|---|---|---|
| Status | enabled | isolated | **disabled** (raises; awaits a separate approved design) |
| Real paths/engines | allowed | refused | - |
| Overlay | `studio.dev.toml` if present | `studio.test.toml` (ComfyUI port 18188) | - |
| Rules | none | `test_sandbox_root` required; all paths, LTX python and ffmpeg/ffprobe inside it; port != 8188; no remote; `allows_live_engines` false | - |

A test must give the sandbox and put everything inside it:
```python
sb = tmp_path  # the explicit sandbox allowlist
StudioSettings(
    env="test",
    test_sandbox_root=sb,
    paths={"workspace_root": sb / "ws", "ltx_repo": sb / "ltx", "ltx_models": sb / "ltx_models",
           "ltx_python": sb / "ltx" / ".venv" / "Scripts" / "python.exe"},
    ffmpeg={"ffmpeg": str(sb / "bin" / "ffmpeg.exe"), "ffprobe": str(sb / "bin" / "ffprobe.exe")},
)
```
`STUDIO_CONFIG_DIR` should name a config folder (the repository's or a temporary copy). `test_sandbox_root`
is rejected outside `env=test`. 8.3 short names in the sandbox path are rejected: use long names.

## Sources and precedence (highest first)
Init kwargs; `STUDIO_*` variables (nested `__`, e.g. `STUDIO_PATHS__WORKSPACE_ROOT`); `studio.<env>.toml`;
`studio.toml`; built-in defaults. Unknown keys are rejected. `get_settings()` caches; call
`get_settings.cache_clear()` after changing the environment.

## Structure
| Section | Content |
|---|---|
| top level | `log_level` (`DEBUG INFO WARNING ERROR CRITICAL`, case-insensitive), `hardware_profile` (validated name), `registry_dir` (absolute), `test_sandbox_root` (test only) |
| `[paths]` | `workspace_root`; `projects_root assets_root outputs_root cache_root logs_root data_root`; `models_root comfyui_root comfyui_output_dir ltx_repo ltx_python ltx_models` |
| `[comfyui]` | `host` (loopback unless `allow_remote`), `port`, `client_id_prefix`, `request_timeout_s` |
| `[ltx]` | `render_script` |
| `[ffmpeg]` | executable names or absolute paths |

Removed (now pinned): `studio_outputs_subdir`, `comfyui.output_subdir`, `ltx.studio_category`, `ltx.name_prefix`.
Defaults: roots derive from `workspace_root` (`E:\AI_VIDEO_STUDIO`); `ltx_repo` `E:\Dev\ltx25-igpu-streaming`
with `.venv\Scripts\python.exe`; `ltx_models` `E:\AI-Data\LTX25`.

## Validation (construction time)
- every configured path is absolute on a lettered drive and passes segment hardening;
- studio roots are strictly inside `workspace_root`, not in `software\`, not nested in each other, and do not
  overlap external roots; external roots do not overlap each other; `comfyui_output_dir` is inside `comfyui_root`;
- `ltx_python` is `python.exe` and not inside a studio root; outside `ltx_repo` is only a `check_environment` warning;
- ComfyUI host is loopback unless `allow_remote`;
- environment rules (table above).

## Access policy and authorization
`settings.access_policy` is the actor/operation policy; `studio.config.guard.authorize` is the mandatory
real-path gate. See security-model.md sections 1-4. No file operation is implemented.

## Environment checks (read-only)
Missing config folder, external directories/files and hardware profile are errors. Absent studio roots are
`info` ("created later"), except `assets_root`, which is "read-only originals; never created by the studio".
`ltx_python` outside `ltx_repo` and any root on a symlink/junction are warnings.

## Hardware profile
`config/hardware/rtx3050-6gb.toml`: ADR-0007 thresholds, **provisional** (not calibrated); `provisional = false`
needs a `calibration_ref`. `system_ram_gb` and `commit_limit_gb` are unset (unknown), so under the
unknown-never-passes rule no binding is admissible against this profile yet. The profile used for admission
is always the one named by `StudioSettings.hardware_profile`.

## Open items
See `docs/adr/0008-configuration-and-registry-design.md` (U-1 ...).
