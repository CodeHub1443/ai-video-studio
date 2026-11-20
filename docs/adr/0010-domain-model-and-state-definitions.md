# ADR-0010: Domain model and state definitions

**Status: PROPOSED, awaiting approval.** Records what M0.3 implemented, revised by the M0.3a design and the
user decisions UD-1..UD-7 and UD-12. Nothing here is accepted. Accepted ADRs (0001-0007) are not modified.
Nothing has been executed, type-checked, linted or tested (A1). The detailed M0.3a decisions are in
[ADR-0011](0011-slots-fingerprints-recovery.md) (PROPOSED). The P-n and U-n below belong to the ADR-0010
series, not to ADR-0008.

## Context
M0.3 needs data contracts for production, workflow and assets plus state vocabularies, independent of engines
and of the pydantic-based configuration, without implementing persistence, scheduling or ingestion.

## A. Taken from accepted text or user decisions (not new proposals)
- `JobState` set, gates as DAG nodes holding no lease, retry taxonomy, "resolved task and seed persisted before
  the first attempt", creative vs execution-only split, Object/Asset/AssetVersion/LineageEdge/Approval concepts,
  approvals becoming stale, append-only history (architecture sections 2, 3, 5, 6; ADR-0004, ADR-0006).
- Unknown requirements are not assumed; unknown rights stay `unknown` (ADR-0008 D-1, architecture section 9).
- Resource adaptation always creates a new take that requires approval (UD-5).
- SHA-256 for objects and task fingerprints (UD-2).
- Approvals go stale on creative-identity change only (UD-3).
- Retry and resume have separate limits from binding or engine policy, with no domain default (UD-4 rejected the
  shared budget; UD-7).
- Slot and SlotBinding exist in the domain and imported assets fill slots (UD-1).

## B. Proposals (PROPOSED; none accepted)
- P-1 *(revised)* State sets and transition tables for project, shot, take, attempt, approval, gate, asset
  version and slot binding, and the job transition edges themselves. Changes: Approval REJECTED is terminal;
  Shot gains APPROVED to READY on a creative-identity change; new SlotBinding table (ACTIVE to SUPERSEDED or
  WITHDRAWN); `WAITING_RESOURCES` to `FAILED` requires a job-level failure record.
- P-2 *(narrowed)* Recovery split: attempt-level (retry, resume) vs take-level (creative regeneration, resource
  adaptation, reproduction), as separate enums. The part "adaptation always creates a new take" moved to A (UD-5).
- P-3 *(replaced)* Canonical form v2: RFC 8785 over a restricted value domain (ADR-0011), replacing the M0.3
  Python-specific canonical form.
- P-4 *(extended)* `ErrorClass` members (infrastructure, resource, invalid_spec, integrity, unknown) and the
  mapping from class to permitted recovery, plus `AttemptEndCause` (ENGINE, STUDIO_PROCESS, HOST_RESTART,
  USER_STOP): an interrupted attempt carries a cause. Under UD-7 (amended): USER_STOP consumes no budget and
  does NOT count toward the crash-loop breaker; STUDIO_PROCESS and HOST_RESTART count toward the breaker
  without consuming budget; engine-side failures consume budget. Planner rulings under UD-7 (PROPOSED with
  this ADR; see ADR-0011): R-1 the breaker counts an interrupt by its interruption time (`finished_at` set at
  marking), not `started_at`; R-2 after USER_STOP there is never an automatic follow-up, only a human
  MANUAL_RESTART; R-3 an attempt interrupted before it started is not recovery: re-dispatch consumes no budget,
  needs no policy limit and never counts toward the breaker; R-4 breaker K and T have no code defaults, values
  live only in `config/studio.toml` (3 / 1800, provisional), absent configuration fails closed (K = 1); R-5 a
  limit of None means that kind is never automatic, a limit of 0 means no budget-consuming follow-ups while
  non-budget follow-ups remain allowed.
- P-5 **WITHDRAWN.** The shared retry budget of 2 was rejected by UD-4. Replaced by the recovery section of
  ADR-0011 (separate limits, no domain default).
- P-6 *(revised)* `studio.domain.vocab` is the single source of `Operation`, `MediaType` and `CommercialUse`;
  the registry imports it (the registry may import the domain, never the reverse). Replaces the M0.3 duplication.
- P-7 *(extended)* Approval authority is `human` or `pre_authorized_policy`, the latter citing a `policy_ref`.
  UD-6 rules: a pre-run adaptation approval may be policy-authorized only when the creative identity is
  unchanged; CONTENT_REVIEW of an adapted take is always human; CONTENT_REVIEW is human-only for all takes for
  now (I-2).
- P-8 *(revised)* Snapshot consistency rules between entity states, now including slots and slot bindings,
  as-of evaluation of approvals, and one current decision per subject and purpose.
- P-9 *(revised)* A take is executed by at most one job. A `Take` carries `identity_hash`, `task_fingerprint`
  and `seed` (all required); the `Job` carries the task and a required recovery policy snapshot.

## C. Open items (not decided)
- U-1 Which pre-authorization policies exist. Direction (I-2): a minimal catalogue, one execution-adaptation
  policy class per binding naming allowed execution keys; CONTENT_REVIEW human-only for all takes for now.
  Remains open until accepted.
- U-2 **Resolved** by UD-5 and UD-6 (an adapted result is a new take; adaptation may change only declared
  execution-only keys, with width, height and duration declarable through an allowlist and never fps: I-1b).
- U-3 **Resolved** by UD-7 (separate limits; interrupted attempts consume no budget unless the cause is ENGINE).
- U-4 **Resolved** by UD-2 and ADR-0011 (SHA-256; envelope and canonical v2).
- U-5 What `unknown` or `no` commercial use / rights blocks (D7-D9). Direction (I-3): block export (not
  generation) of commercial deliverables when commercial use is "no" or "unknown", and warn in drafts. Not
  implemented and not decided.
- U-6 **Resolved** by UD-1 and ADR-0011. Deliverable slots and frame extraction come with the timeline
  milestone (I-11).
- U-7 Quarantine/staging may exist only on disk; whether those asset states are ever persisted is a
  persistence decision.
- U-8 Plan-apply semantics for shot/take/gate states: **partly addressed** by the invalidation matrix of
  ADR-0011.
- U-9 **Resolved** by P-6 (one vocabulary source).
- U-10 Nothing has been run; transition tables, canonical form and validators are unverified.

## Consequences
- Later milestones (persistence, scheduler, executors) can import `studio.domain` without pulling in pydantic or engines.
- Changing any state table or the canonical rules is a deliberate, versioned act.
- If this ADR is not accepted, the M0.3 text (including the shared budget of 2) contradicts UD-4 and UD-7.
