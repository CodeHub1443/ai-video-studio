# Registry

Code: `src/studio/registry/`. Data: `config/registry/{engines,models,bindings}/*.{yaml,yml}`.

The registry **declares** what exists and what it can do. It does not start, probe or call anything.
Live probes (ComfyUI `/object_info`, ffmpeg encoders, LTX CLI options) belong to the doctor, later: a
capability is the declaration intersected with a live probe (docs/architecture.md section 3 lists the
engine contract; the probe step itself is not designed yet).

## Three kinds of entry (one YAML document per file, `schema_version: 1`)
| Kind | Describes | Key fields |
|---|---|---|
| **Engine** | A backend | `id`, `kind` (service / process / tool), `adapter` (dotted name under `studio.engines.`, never imported), `operations`, `probes` |
| **Model** | Metadata about model files | `id`, `kind`, `family`, `format`, `quantization`, `license`, `files[] {root, path, role, size_bytes?, sha256?}` |
| **Binding** | One capability: engine + models (+ template) for exactly one operation | `id`, `engine`, `operation`, `models`, `inputs`, `outputs`, `constraints`, `features`, `determinism`, `execution_only`, `resources`, `template` |

`schema_version` has a single source (`studio.versions`) shared with hardware profiles. File name = id
(`:` written as `_`); a mismatch is a warning. `.yaml` and `.yml` are both read; other files are reported
as ignored. Ids are lowercase, segments joined by `.` or `:`. Loaded specs are immutable (tuples, read-only
mappings).

## Model metadata is separate from model files
A model names files only as `{root, path}`: `root` is a logical key (`models` or `ltx_models`) and `path` is
relative with forward slashes. `.` alone means the whole root and is accepted only for `format: directory`.
Segments are hardened (no `..`, no `:`/drives/NTFS streams, no reserved names, no 8.3 aliases; see
security-model.md section 5).
`resolve_model_file(ref, paths)` is a pure lexical join; `check_model_files(registry, paths)` is an
optional read-only existence/size check that warns about links. Loading a registry never touches model files.

## Resource requirements
`resources` carries `gpu` (`none | exclusive | media`) and four `Quantity` values: `vram_gb`, `ram_gb`,
`commit_gb`, `est_minutes`. A quantity is `{value, basis, source}`:

| basis | meaning |
|---|---|
| `unknown` | no value (`value: null`); the default |
| `observed` | one reported observation; needs a `source`; not calibrated |
| `file_size` | derived from model file sizes; needs a `source` |
| `measured` | recorded by an authorized calibration run (M3.x); needs a `source` |

There is no "placeholder" or "guess" basis: a number nobody measured is `null`. **The M0.1 guesses (VRAM
5.0/5.5 GB, RAM 8/16/2 GB, commit 8/24/2 GB, ComfyUI 1 min) were removed**; the only value left is the LTX
`est_minutes` observation (5.8 min, one run, stage 1 only, 256x384; provenance in the file).
`estimate_includes_context` feeds `new_context_term` (ADR-0007) and may be set only once `vram_gb` is
known; unset is treated as "context not included" (conservative).

### Admission and hardware compatibility
`studio.registry.compat` checks a registry against the hardware profile named by `StudioSettings`
(*approved*; callers pass settings, never a profile object):
- **unknown never passes.** A binding is admissible only if every hard-gate requirement it declares is known:
  VRAM (GPU classes), RAM and commit (all bindings, CPU and GPU). Any unknown blocks it
  (`vram_unknown`, `ram_unknown`, `commit_unknown`); every shipped binding is currently blocked;
- a known requirement is compared with the profile: `vram + new_context_term + vram_margin <=
  gpu_vram_total_gb` (term 0 for the resident ComfyUI service or when the estimate includes the context, else
  `default_cuda_context_mb`); `ram + ram_reserve_gb <= system_ram_gb`; `commit + commit_reserve_gb <=
  commit_limit_gb`. If the profile leaves `system_ram_gb`/`commit_limit_gb` unset, the binding is blocked
  (`profile_ram_unknown`, `profile_commit_unknown`);
- `est_minutes` is not a hard gate; thresholds still provisional are reported (`thresholds_provisional`).
This is not the runtime decision, which uses live NVML/RAM readings (ADR-0007). Nominal total VRAM,
`gpu_slots` and `allow_media_during_inference` are not modelled (U-7).

## Validation
On load: YAML parse (safe loader, duplicate keys rejected, size limit, symlinked files/folders rejected),
schema (unknown fields rejected), duplicate ids. Then cross-checks:
- an empty registry is an error (no engines, or no bindings);
- binding -> engine exists and declares the operation; binding -> models exist;
- tool engines use gpu `none` or `media`; `media` only for tools; a GPU binding may leave `vram_gb` unknown but never declares 0; gpu `none` declares no non-zero `vram_gb`;
- required inputs per operation (I2I `init_image`, INPAINT `init_image`+`mask`, I2V `start_frame`, V2V/UPSCALE/INTERP/TRANSCODE `clip`, ASR `audio`, RENDER_TIMELINE `timeline`);
- **input/output media compatibility**: each input and output media type must be allowed for the operation
  (for example `t2i` cannot output video, `asr` outputs text, `render_timeline` outputs video); the table is a
  provisional policy in `loader.py` (proposal P-5, awaiting approval); features must match slots (`mask`, `reference_images`, `native_audio`);
- unique input roles and output roles and `execution_only` keys; constraint ranges.
Warnings: unknown VRAM/RAM/commit, unresolved templates, models unused by any binding, engines without bindings.
All problems are collected into one `RegistryError`.

## Templates (deferred to M2.1)
Workflow-template resolution is not implemented. A binding may carry only
`template: {status: unresolved, hint: ...}`; no path or hash is accepted, so the registry holds no path that
looks real but points at nothing. M2.1 adds the resolved form.

## Minimal example (binding)
```yaml
schema_version: 1
id: ltx_streaming.ltx25.i2v
display_name: LTX-2.5 image-to-video with native audio
engine: ltx_streaming
operation: i2v
models: [ltx25_22b]
inputs:  [{role: start_frame, media_type: image, required: true}]
outputs: [{role: video, media_type: video}]
constraints: {dim_multiple: 32, frame_rule: "8k+1"}
execution_only: [SYNC_S1, TEMP, TMP, HF_HOME]
resources:
  gpu: exclusive
  vram_gb: {value: null, basis: unknown}   # unknown until measured: not admissible
```
Usage: `reg = load_registry(settings.effective_registry_dir)`; `reg.bindings_for(Operation.I2V)`;
`check_hardware_compatibility(reg, settings)`.

## Shipped entries
Engines `comfyui`, `ltx_streaming`, `ffmpeg`; models `z_image_turbo_q5`, `qwen3_4b_q8`, `ae_vae`,
`ltx25_22b`; bindings `comfyui.z_image_turbo.t2i`, `ltx_streaming.ltx25.{t2v,i2v}`, `ffmpeg.cpu.transcode`.
Facts marked `UNVERIFIED` in the YAML comments (ComfyUI `dim_multiple`, whether `SYNC_S1` changes output,
ffmpeg determinism) are assumptions, not findings.
