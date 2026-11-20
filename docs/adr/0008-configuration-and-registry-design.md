# ADR-0008: Configuration and registry design

**Status: PROPOSED, awaiting approval.** This ADR as a whole is not accepted. It records (A) the points the
user has directed or approved, (B) the implementer's own proposals that nobody has approved, and (C) open
items. Nothing here has been executed, type-checked, linted or tested. Accepted ADRs (0001-0007) are not
modified by it; proposed changes to them are in ADR-0009.

## Context
M0.2 needs typed settings and a model/capability registry consistent with ADR-0001..0007 under A1 (no
execution, no I/O at construction time that touches external systems). Two review rounds of the M0.2 draft
led to the points below.

## A. Directed or approved by the user
Source: the M0.2 revision brief, the six checks of the verification brief, and the eight decisions of the
final-revision brief. The implementer did not receive the original review text; each item is quoted from
those briefs.

From the M0.2 revision brief (required changes):
- A-1 `assets\` is read-only originals; studio output writes are confined to a dedicated subfolder.
- A-2 Explicit external write-scope declarations and guards; only the ComfyUI output and LTX video namespaces.
- A-3 Resource values may be `null` with explicit provenance; no fabricated estimates; registry-to-hardware
  validation; provisional thresholds kept and not presented as calibrated.
- A-4 Adapter ids restricted to `studio.engines.*`; hardened path segments (drives, NTFS streams); symlink
  and junction risk in the security model; input/output compatibility validation; reject empty registries;
  support `.yml`; template resolution deferred to M2.1 with unresolved references marked.
- A-5 Validated `STUDIO_ENV`; distinct dev/test/prod behaviour; constructor/overlay mismatch fixed.
- A-6 Consistent schema versions; no mutable nested values in frozen models; `log_level` and `ltx_python`
  validation corrected; package-installed config-path assumption fixed; stale references and README fixed.

From the final-revision brief (eight decisions):
- D-1 Admission is blocked whenever any hard-gate requirement is unknown: VRAM, RAM and commit headroom,
  for CPU and GPU workloads according to their declared requirements.
- D-2 All approved output namespaces are pinned; configurable values that could broaden them are removed.
- D-3 Actor/operation separation: ComfyUI and LTX own writes to their namespaces; the studio may read/import
  there but never writes, deletes or move-source-deletes in external engine directories; studio writes stay
  in approved studio roots.
- D-4 Real-path and reparse-point validation is mandatory before any future write; symlink/junction escapes
  are rejected; a TOCTOU-safe design is documented; no file operation is implemented.
- D-5 `dev` enabled; `test` isolated (temporary roots, no live engines, no production data); `prod` disabled
  pending a separate approved design.
- D-6 Test isolation uses explicit allowlisted sandbox paths and rejects real model, ComfyUI, LTX, Python and
  FFmpeg locations.
- D-7 Hardware compatibility is bound to the profile selected through `StudioSettings`; no arbitrary profile
  injection at the admission boundary.
- D-8 Accepted ADRs are immutable; changes go through a superseding ADR or a clearly marked proposed amendment.

## B. Implementer proposals (NOT approved)
- P-1 Segment hardening details beyond drives/streams: reserved device names, 8.3 short-name rejection,
  trailing dot/space, length limits.
- P-2 `authorize` refuses mutating operations on existing files with more than one hard link.
- P-3 An empty `STUDIO_ENV` is an error rather than "unset".
- P-4 Config-folder resolution: `STUDIO_CONFIG_DIR`, else a source checkout detected by `pyproject.toml` +
  `studio.toml`, else none.
- P-5 The per-operation allowed input/output media table in `loader.py`.
- P-6 `ltx_python` must be named `python.exe` and not lie in a studio root, but may be outside `ltx_repo`.
- P-7 Test sandbox must be at least two levels below the drive root; `registry_dir` is not confined by it;
  `test_sandbox_root` is rejected outside test.
- P-8 `Quantity` bases (`unknown`, `observed`, `file_size`, `measured`) and the rule that a value needs a source.
- P-9 Registry loader: 1 MB file cap, symlinked files/folders rejected, other files reported as ignored.
- P-10 Read access to the protected LTX folders is denied for every actor until D11 is decided.
- P-11 A hardware profile may be `provisional = false` only with a `calibration_ref`.
- P-12 `log_level` accepts any case, plus `CRITICAL`, `WARN` and `FATAL` aliases.
- P-13 `.` as a model path only for `format: directory`.
- P-14 The `Authorization` object and `revalidate` as the shape of the mandatory gate (D-4 fixes the
  requirement, not this API).

## C. Open items (not decided; nothing here is presented as fact)
- U-1 `prod` behaviour: needs its own design and approval (disabled until then).
- U-2 D6 (hardlink vs copy-always) is open, and ADR-0006's "move" and hardlink read-only-attribute steps
  conflict with D-3 (see ADR-0009).
- U-3 Source gap: the original review text and the original "six decisions" list were not available to the
  implementer; group A was reconstructed from the briefs.
- U-4 The pinned ComfyUI/LTX namespaces restate ADR-0002/0003 and are unverified against what the engines
  actually write.
- U-5 `registry_dir` and the config folder are read-only inputs not confined by the test sandbox.
- U-6 Nothing proves `test_sandbox_root` is a temporary directory; 8.3 short names in a temp path would be
  rejected.
- U-7 Static compatibility does not model nominal-vs-usable VRAM, `gpu_slots` or
  `allow_media_during_inference`. `system_ram_gb` and `commit_limit_gb` are unset in the shipped profile, so
  no binding is admissible yet.
- U-8 D11 (import of protected perfume files) and D7-D9 (delivery, audio, licenses; all model licenses are
  `unknown`).
- U-9 The environment-selection code relies on pydantic-settings internals (`init_kwargs`, TOML source
  merge behaviour) that could not be verified without running anything.
- U-10 LTX `est_minutes = 5.8` is an owner-reported observation; engine facts in the YAML marked
  `UNVERIFIED` are assumptions.
- U-11 The TOCTOU protocol (security-model.md section 4) is a design, unimplemented and unverified.
- U-12 ADR-0003's "anything tracked by Git" deny cannot be represented without Git access; directories above
  a scope root (workspace bootstrap) are outside every scope.
- U-13 Nothing in M0.2 has been executed, type-checked, linted or tested.

## Consequences
- Settings, access policy and registry are testable without devices once execution is authorized.
- Every shipped binding is currently not admissible, by design, until requirements are measured and the
  profile states RAM and commit limits.
- Test configurations must name a sandbox and place every path and tool inside it.
