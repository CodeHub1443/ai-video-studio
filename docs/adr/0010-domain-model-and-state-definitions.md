# ADR-0010: Domain model and state definitions

**Status: PROPOSED, awaiting approval.** Records what M0.3 implemented. Accepted ADRs (0001-0007) are not
modified. Nothing has been executed, type-checked, linted or tested (A1).

## Context
M0.3 needs data contracts for production, workflow and assets plus state vocabularies, independent of engines
and of the pydantic-based configuration, without implementing persistence, scheduling or ingestion.

## A. Taken from accepted text (not new)
- `JobState` set, gates as DAG nodes holding no lease, retry taxonomy, "resolved task and seed persisted before
  the first attempt", creative vs execution-only split, Object/Asset/AssetVersion/LineageEdge/Approval concepts,
  approvals becoming stale, append-only history (architecture sections 2, 3, 5, 6; ADR-0004, ADR-0006).
- Unknown requirements are not assumed; unknown rights stay `unknown` (ADR-0008 D-1, architecture section 9).

## B. Implementer proposals (NOT approved)
- P-1 State sets and transition tables for project, shot, take, attempt, approval, gate and asset version, and the
  job transition edges themselves (the architecture lists job states, not edges).
- P-2 Recovery split: attempt-level (retry, resume) vs take-level (creative regeneration, resource adaptation,
  reproduction), as separate enums; resource adaptation and reproduction always create a new take linked to its source.
- P-3 Canonical form rules (`canonical.py`): sorted-key compact UTF-8 JSON, type-preserving numbers, 64-bit
  integers, no Unicode normalization, resolved form uses content hashes of inputs, seed mandatory.
- P-4 `ErrorClass` members (infrastructure, resource, invalid_spec, integrity, unknown) and the mapping from class
  to permitted recovery; an interrupted attempt without a class counts as infrastructure.
- P-5 The retry budget (default 2, from the architecture's "max 2") bounds retry and resume attempts together.
- P-6 The domain duplicates `Operation`, `MediaType` and `CommercialUse` by value instead of importing the registry.
- P-7 Approval authority is `human` or `pre_authorized_policy`; the latter must cite a `policy_ref`.
- P-8 Snapshot consistency rules between entity states (e.g. approved shot needs approved selected take).
- P-9 A take is executed by at most one job; the `Take` carries `spec_hash` and `seed`, the `Job` carries the task.

## C. Open items (not decided)
- U-1 Which pre-authorization policies exist (preview tier, QC gates) and what they may approve. The domain records a
  `policy_ref` and nothing else.
- U-2 Whether a resource-adapted result is a new take (implemented) or a variant of the same take; and what an adaptation
  may change (resolution, frames, offload) versus what needs a creative decision.
- U-3 Whether resume has its own budget, and whether interrupted attempts consume retry budget.
- U-4 The hash function and `spec_hash` encoding (no function is invoked; only canonical bytes exist).
- U-5 What `unknown` or `no` commercial use / rights blocks (D7-D9). Rights are metadata only.
- U-6 Slot and SlotBinding (architecture section 6) are not modelled.
- U-7 Quarantine/staging may exist only on disk; whether those asset states are ever persisted is a persistence decision.
- U-8 Plan-apply semantics for shot/take/gate states (which states stale-mark which) are only approximated by transitions.
- U-9 Parity of the duplicated vocabularies with the registry needs a test once execution is authorized.
- U-10 Nothing in M0.3 has been run; transition tables, canonical form and validators are unverified.

## Consequences
- Later milestones (persistence, scheduler, executors) can import `studio.domain` without pulling in pydantic or engines.
- Changing any state table or the canonical rules is a deliberate, versioned act.
