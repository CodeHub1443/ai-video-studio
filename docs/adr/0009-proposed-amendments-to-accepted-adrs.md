# ADR-0009: Proposed amendments to ADR-0003, ADR-0006 and ADR-0007

**Status: PROPOSED, awaiting approval.** Accepted ADRs stay unchanged until this is approved; if it is,
it is recorded as a superseding ADR for the parts it replaces (decision D-8 of ADR-0008). Until then the
text of ADR-0003, 0006 and 0007 is the accepted position, and the code and docs of M0.2 follow the user's
decisions where they differ, so the differences are listed here.

## Context
The final M0.2 decisions (ADR-0008 group A) forbid the studio from writing, deleting or move-source-deleting
in external engine directories, and require unknown hard-gate requirements to block admission. Some accepted
text predates them.

## Proposed amendments

### To ADR-0006 (asset ingestion)
1. **Move becomes copy for external sources.** The table row "ComfyUI studio outputs: Move" and the step
   "dispose of the source" would delete files in ComfyUI's output folder, which the studio may no longer do.
   Proposed: ComfyUI and LTX outputs are always **copied** into the object store; the studio never deletes
   the engine-side names. Cleaning engine folders is a separate, explicitly authorized action outside the
   studio's write scopes (or by the user).
2. **Hardlink mode is unavailable as written.** Setting the read-only attribute "shared by both names"
   changes the external file, and a hardlink of an engine file would share its mutability. Proposed: treat
   D6 as "copy-always" unless a later decision approves hardlinks with an explicit external-write exception.
3. **`assets\` is read-only originals**: never a destination, move source or hardlink source (already "copy
   always" in the table; stated explicitly).
4. The ingestion protocol would call `studio.config.guard.authorize` for every studio-side write and
   `AccessPolicy.check` for every external read.

### To ADR-0003 (LTX via unchanged CLI)
1. The path guard becomes the pinned constants in `studio.config.namespaces` plus the actor/operation policy
   in `studio.config.scopes`; namespace values are not configurable.
2. LTX owns writes to `videos/studio/**` and `{latents,logs,previews}/studio-*`; the studio only reads them.
3. `episodes/**` and `prompts/**` are closed to every actor (D11 undecided). The clause "anything tracked by
   Git" cannot be represented without Git access and remains a run-time obligation of a later milestone.

### To ADR-0007 (GPU admission)
1. Add: an unknown VRAM, RAM or commit requirement of a binding never passes admission (static check in
   `studio.registry.compat`; the runtime resource manager must obey the same rule). This sharpens, and does
   not contradict, point 4 ("err toward waiting").
2. Add: the profile used is the one selected through `StudioSettings`; an unset `system_ram_gb` or
   `commit_limit_gb` blocks any binding that must be compared against it.

## Consequences
- If approved, ingestion costs extra disk space for video (copy instead of move/hardlink).
- If not approved, the code's actor rules and ADR-0006's text conflict and a later milestone must resolve it
  before implementing ingestion.

## Not decided here
D6 (hardlink choice) remains open; D11 (protected perfume files) remains open.
