# ADR-0006: Asset ingestion with conditional hardlinks

Status: Accepted

## Context
Generated media is large and produced by engines that write into their own trees. Assets must be immutable,
verifiable and traceable. C:, D: and E: are NTFS; the studio and the LTX repository are both on E:.

## Decision
Objects are immutable, content-addressed (SHA-256) files under `projects/<slug>/store/objects/sha256/ab/cd/`.

Ingestion protocol: wait for the producer's completion signal and a stability check -> path guard -> stage
into `store/staging/<ulid>.part` on the same volume, computing SHA-256 while streaming the staged bytes ->
fsync -> probe/validate (failures go to `quarantine/`) -> deduplicate -> atomic `os.replace` and set
read-only -> one DB transaction (object, asset, version, lineage, run outputs, attempt success) -> dispose
of the source. Startup `fsck` removes stale `.part` files and lists orphans (never auto-deleted).

Transfer modes:

| Source | Mode |
|---|---|
| ComfyUI studio outputs (same volume) | Move |
| LTX studio-namespace outputs (same volume, unique write-once names) | Hardlink, falling back to copy |
| User originals, `assets/` library, other volumes, protected LTX files, deliverable exports, non-NTFS sources | Copy, always |

Hardlinks are allowed only for producer-unique, write-once paths because an in-place write through either
name would mutate the object. The studio records file ID and volume serial and sets the read-only attribute,
which both names share (this makes the LTX-side studio-namespace file read-only by design). Export verifies
hash and file ID. The studio never deletes LTX-side names; cleaning is a separate authorized action.

Identity model: Object (hash), Asset (logical thing), AssetVersion (immutable revision), Slot (role in the
production), SlotBinding (append-only history), LineageEdge (append-only), Approval (attached to takes or
versions, may become stale).

## Consequences
- Space is saved for video without risking silent mutation of originals.
- Integrity errors block export and are visible.
- Garbage collection is postponed and always dry-run first.
- Open decision D6: choose hardlink (including the shared read-only attribute) or copy-always.
