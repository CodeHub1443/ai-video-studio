# AI Video Studio

Local-first production studio for commercial video, controlled from Python. It coordinates
ComfyUI (images), the independent LTX-2.5 streaming runtime (video + audio), and FFmpeg
(post-production) on a single-user Windows machine with a 6 GB GPU.

**Status: M0.3 (source and documentation only; awaiting review).** M0.1 laid the foundation, M0.2 added
configuration and the registry, M0.3 added the domain model and state definitions (`src/studio/domain/`,
`docs/domain-model.md`, ADR-0010 proposed). Nothing is installed, no environment exists, and **no code
has been executed, type-checked, linted or tested**. M0.4 has not started. Every resource requirement is
currently unknown, so no binding is admissible. `prod` is disabled. ADR-0008 and ADR-0009 are **Proposed,
awaiting approval**; ADR-0001..0007 are accepted and unchanged. See `docs/architecture.md`, `docs/adr/` and
the open items in `docs/adr/0008-configuration-and-registry-design.md`.

## Layout (current)
```
pyproject.toml          dependency declarations (Python 3.12, no torch, no lockfile yet)
src/studio/versions.py  single schema version
src/studio/config/      settings, paths, pinned namespaces, access policy, real-path guard, hardware profile, checks (M0.2)
src/studio/registry/    engine/model/binding schema, loader, validator, settings-bound admission check (M0.2)
src/studio/domain/      stdlib-only entities, state tables, recovery rules, canonical form, snapshot validation (M0.3)
config/                 studio.toml, studio.test.toml, hardware/, registry/{engines,models,bindings}/ (M0.2)
docs/                   architecture.md, configuration.md, registry.md, security-model.md, domain-model.md, adr/ (0001-0010)
```
Not yet present (later milestones): `workflows/`, `tests/`, and the packages
`src/studio/{core,persistence,engines,resources,scheduler,workflows,assets,qc,audio,post,review,plan,cli}`.
The wheel does not ship `config/`; an installed package must set `STUDIO_CONFIG_DIR`.

## Authorization model
Work proceeds in explicitly granted levels (A0 design ... A8 VCS/destructive). M0.1 and M0.2 were A1
(author code and docs under this folder only): no venv, installs, execution, tests, network, Git, or
changes to ComfyUI/LTX.

## Hard rules
- ComfyUI is externally managed; the studio never starts, stops or reconfigures it.
- The LTX repository (`E:\Dev\ltx25-igpu-streaming`) is used only through its unchanged CLI and is never
  modified by the studio. ComfyUI and LTX write only to their pinned namespaces; the studio only reads them
  and never writes, deletes or moves from external engine directories (`docs/security-model.md`).
- `assets\` holds read-only originals; the studio writes only to its own roots and `outputs\studio`.
- Any future write must pass real-path and reparse-point validation (`studio.config.guard.authorize`).
- Only one heavy GPU workload runs at a time.
