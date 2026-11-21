# ADR-0009: Proposed amendments to ADR-0003, ADR-0004, ADR-0006 and ADR-0007

**Status: PROPOSED, awaiting approval.** Nothing here is accepted. Accepted ADRs stay unchanged until this is
approved; if it is, it is recorded as a superseding ADR for the parts it replaces (decision D-8 of
[ADR-0008](0008-configuration-and-registry-design.md)). Until then the text of ADR-0003, 0004, 0006 and 0007
is the accepted position, and the code and docs follow the user's decisions where they differ, so the
differences are listed here. Letters (a)-(f) refer to the M0.3a design (docs/design/m0.3a-domain-amendments.md,
section F.2). The user's resolutions of the open questions are cited as I-n (UD-12).

## Context
The final M0.2 decisions (ADR-0008 group A, accepted) forbid the studio from writing, deleting or
move-source-deleting in external engine directories, and require unknown hard-gate requirements to block
admission. Some accepted text predates them. The M0.3a decisions (UD-1, UD-3, UD-4, UD-7) add the slot
identity model, creative-identity staleness and per-binding recovery limits; these are recorded in
[ADR-0011](0011-slots-fingerprints-recovery.md) (PROPOSED).

## Proposed amendments

### To ADR-0006 (asset ingestion)
1. **Move becomes copy for external sources.** The table row "ComfyUI studio outputs: Move" and the step
   "dispose of the source" would delete files in ComfyUI's output folder, which the studio may no longer do.
   Proposed: ComfyUI and LTX outputs are always **copied** into the object store; the studio never deletes
   the engine-side names. Cleaning engine folders is a separate, explicitly authorized action outside the
   studio's write scopes (or by the user).
2. **(a) Hardlink mode is suspended; copy-always through M0.x by user decision (I-4).** Setting the shared
   read-only attribute writes to an engine-owned file, which ADR-0008 D-3 forbids, and a hardlink of an engine
   file would share its mutability. Ingestion copies. Hardlinks may be reconsidered only with an explicit
   external-write exception, a guard exception (item 5) and measured disk pressure, after M0.x.
3. **`assets\` is read-only originals**: never a destination, move source or hardlink source (already "copy
   always" in the table; stated explicitly).
4. The ingestion protocol would call `studio.config.guard.authorize` for every studio-side write and
   `AccessPolicy.check` for every external read.
5. **(b) Hardlink and the multi-link guard.** A hardlinked object has a link count of 2, so `authorize`
   (ADR-0008 P-2) refuses WRITE/DELETE on it. Garbage collection of such objects would be refused, and under
   the READ rule of ADR-0008 U-16 (I-13) studio-scope READs too. Adopting hardlinks therefore also requires a
   deliberate guard exception.
6. **(new, UD-1) Identity model.** "Slot (an owner-scoped production position: role, direction, media type,
   required or optional, cardinality, constraints); SlotBinding (append-only history binding one AssetVersion,
   generated or imported, to a slot, with optional take provenance and supersession); Approval (attached to
   takes, versions, **slot bindings** or gates; may become stale per
   [ADR-0011](0011-slots-fingerprints-recovery.md))."

### To ADR-0004 (global SQLite database and per-project stores)
1. **(c) Staleness.** Replace "it archives and marks approvals stale" with: "it archives, and marks stale
   exactly those approvals whose subject's *creative identity* changed, as defined by the invalidation matrix of
   ADR-0011. A change limited to execution settings, environment (model, template or engine version) or
   recovery policy does not make approvals stale (UD-3, I-1a); it is recorded as an advisory." The rest of the
   clause (append-only, never deletes) is unchanged.

### To ADR-0003 (LTX via unchanged CLI)
1. The path guard becomes the pinned constants in `studio.config.namespaces` plus the actor/operation policy
   in `studio.config.scopes`; namespace values are not configurable.
2. LTX owns writes to `videos/studio/**` and `{latents,logs,previews}/studio-*`; the studio only reads them.
3. `episodes/**` and `prompts/**` are closed to every actor (D11 undecided). The clause "anything tracked by
   Git" cannot be represented without Git access and remains a run-time obligation of a later milestone.
4. **(e) Engine input reads and scratch.** The engine needs READ access to studio-provided inputs (prompt and
   start-image files in studio-owned folders) and a location for TEMP/TMP/HF_HOME redirects. The user chose
   the direction (I-7): a studio-owned *handoff* scope that the engine may READ, and an engine-owned *scratch*
   scope under the studio cache root that the engine may WRITE, with Hugging Face set offline so that the
   redirect cannot enable downloads. These variables are *managed environment*, not task parameters. **An
   engine-owned scratch scope is a new engine namespace inside a studio root. It therefore also amends the
   accepted ADR-0008 D-2 (pinned namespaces) and D-3 (actor/operation separation), and it changes the rule
   that engine scopes lie inside the engine tree. This amendment is PROPOSED and takes effect only when the
   user accepts this ADR.** The no-amendment alternative is to leave TEMP and HF unredirected.

### To ADR-0007 (GPU admission)
1. Add: an unknown VRAM, RAM or commit requirement of a binding never passes admission (static check in
   `studio.registry.compat`; the runtime resource manager must obey the same rule). This sharpens, and does
   not contradict, point 4 ("err toward waiting").
2. Add: the profile used is the one selected through `StudioSettings`; an unset `system_ram_gb` or
   `commit_limit_gb` blocks any binding that must be compared against it.
3. **(f) Calibration exception.** A binding with an unknown requirement may run only as an explicitly
   authorized calibration job (a brief granting A5/A6/A7), with the GPU idle and leased exclusively, a watchdog
   and conservative stop conditions. Its results are recorded with basis `measured`. Unknown requirements never
   pass *normal* admission. The user chose this direction (I-6). **It also amends the accepted ADR-0008 D-1**
   ("admission is blocked whenever any hard-gate requirement is unknown"). The amendment is PROPOSED and takes
   effect only when the user accepts this ADR; D-1 stands until then.
4. **(f) Refusal route.** A permanent admission refusal fails the job with error class `RESOURCE` and a refusal
   reason. `EXCEEDS_CAPACITY` permits resource adaptation (a new take, UD-5) only for a job that has a take
   (I-12). `UNKNOWN_REQUIREMENT` and `PROFILE_INCOMPLETE` permit no adaptation; the route is calibration.

### Note on docs/architecture.md (d)
"Max 2" is superseded by UD-4 (the shared budget was rejected) and UD-7 (separate per-binding limits, no
default; see ADR-0011). architecture.md is not an ADR; its text is corrected directly after the implementation
merges, and this note records why.

## Consequences
- If approved, ingestion costs extra disk space for video (copy instead of move or hardlink).
- If approved, ADR-0008 D-1, D-2 and D-3 are amended as stated above; until then they are unchanged.
- If not approved, the code's actor rules and ADR-0006's text conflict and a later milestone must resolve it
  before implementing ingestion.

## User decisions recorded here (not ADR acceptance)
- D6 (hardlink choice) is decided for M0.x by I-4: copy-always. Hardlinks after M0.x remain open and are
  revisited with measured disk pressure.

## Not decided here
- D11 (protected perfume files) remains open.
- D7-D9 (delivery, audio, licenses) remain open. The direction chosen in I-3 is to block *export* (not
  generation) of commercial deliverables when commercial use is "no" or "unknown", and to warn in drafts. It is
  not implemented and not an accepted rule.
