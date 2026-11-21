# ADR-0011: Slots, fingerprints, approval invalidation and recovery policy

**Status: PROPOSED, awaiting approval.** Nothing in this ADR is accepted. It records the M0.3a domain design
(docs/design/m0.3a-domain-amendments.md, sections A-D, the frozen design record), amended by user decisions
UD-7 (amended) and UD-12 (I-1..I-8, I-10..I-13; I-9 superseded by UD-7 amended). Implemented by the M0.3a domain and config/registry changes (branches feat/b08-domain-m03a-p1 and feat/b09-config-registry-m03a). This ADR stays PROPOSED regardless of merge state and records no test results. It cites symbols and design sections, not code lines. It amends no accepted ADR by itself; the proposed amendments to accepted ADRs are in
[ADR-0009](0009-proposed-amendments-to-accepted-adrs.md). Labels: UD-n are user decisions, I-n are the
user's resolutions of the design's open questions (UD-12), R-n are planner rulings (R-1..R-5 and R-8..R-12). ADR-0008 (D-n, P-n, U-n)
and ADR-0010 (P-n, U-n) are separate series and are always named.

## Context
User decisions that this ADR gives a form:
- UD-1 Slot and SlotBinding exist in the domain; imported assets fill slots.
- UD-2 SHA-256 for objects and task fingerprints.
- UD-3 Approvals go stale only when the creative identity changes.
- UD-4 The shared retry+resume budget of 2 is rejected; nothing may assume 2.
- UD-5 Resource adaptation always creates a new Take that requires approval.
- UD-6 Pre-run adaptation approval by policy only for declared execution-only changes; an adapted result is
  always human-reviewed.
- UD-7 (amended) Separate retry and resume limits per binding or engine policy with no domain default; crash-loop
  breaker; persisted counters; policy snapshot; idempotent startup marking. Amendment: USER_STOP consumes no
  budget and does NOT count toward the breaker; STUDIO_PROCESS and HOST_RESTART interruptions count toward the
  breaker without consuming budget; engine-side failures consume budget. The design's recommendation to keep
  USER_STOP in the breaker count (design I-9) is superseded.

The M0.3 domain had one mutable `Shot.selected_take_id`, no fingerprint function, one shared budget of 2, no
route from an admission refusal to adaptation, and approvals that went stale on any change.

## Decision (all PROPOSED)

### 1. Slots and slot bindings (UD-1; design section A, alternative A-2)
- A `Slot` is an owner-scoped production position: owner kind (PROJECT, SCENE, SHOT), role, direction
  (INPUT, OUTPUT, MEMBER), media type, required or optional, cardinality, constraints, and whether a selection
  approval is required. The DELIVERABLE owner kind comes with the timeline milestone (I-11).
- Cardinality is ONE or ORDERED_MANY (I-5). Unordered collections are not introduced.
- A `SlotBinding` is append-only history. It binds one AssetVersion (generated, derived or imported) pinned by
  SHA-256 to a slot, with optional take provenance, optional upstream binding (continuity) and supersession.
  A binding is never edited: rebinding supersedes, unbinding withdraws.
- Who binds (I-5): workflows may create continuity bindings; humans make all other bindings. SELECTION approval
  (new `ApprovalPurpose.SELECTION` on the new `ApprovalSubjectKind.SLOT_BINDING`) is always human.
- `Shot.selected_take_id` is removed. The selected take is the `take_id` of the active binding of the shot's
  required OUTPUT slot. Nothing is persisted yet, so no migration is needed.
- Frame extraction for cross-shot continuity needs a new `Operation` and comes with the timeline milestone
  (I-11). Until then a continuity binding may use any imported or derived image version.
- Slot definitions carry structural checks (always) and readiness checks (a separate pure function). The check
  codes are listed in design section A.7.

### 2. Hashing and fingerprints (UD-2, UD-3; design section B, alternative B-3)
- Three digest families: the object digest (SHA-256 of raw bytes, bare 64-hex), the creative identity hash
  `identity_hash`, and the task fingerprint `task_fingerprint`. Further kinds: execution spec, recovery policy
  and stage key.
- Envelope: `{"alg": "sha256", "canonical_version": 2, "kind": <kind>, "payload": <payload>}`. Fingerprints of
  a different kind or canonical version are never equal and never compared by digest.
- Creative identity payload: operation, capability (the registry binding id), prompt, negative prompt, width,
  height, fps (reduced rational), duration in milliseconds, seed (decimal string), references sorted by
  `(role, index)`, and creative free parameters. It excludes database ids, execution parameters, model,
  template and engine identity, and recovery policy.
- Adaptable fields (I-1b): a registry binding may declare `width`, `height` and `duration_s` execution-only
  through a fixed allowlist; those fields then leave the identity payload and move to the execution component
  of the task fingerprint. `fps` is never adaptable.
- Task fingerprint payload: identity digest, execution parameters, models (id, role, SHA-256, size; directory
  models by manifest digest), template digest, engine id and version. Managed environment values are excluded.
  A fingerprint with an unknown component is `complete = false` (a binding that uses a template must always supply its digest; template_sha256 = None means the binding has no template, which counts as known): it is stored but never used for caching,
  deduplication or resume compatibility.
- Canonical form v2 (`CANONICAL_VERSION = 2`): RFC 8785 (JCS) over a restricted value domain. Integers are limited
  to +-(2^53-1); only the seed is encoded as a decimal string, and every other integer stays within
  +-(2^53-1). Floats follow RFC 8785 number formatting; the range applies to integers (R-12, planner ruling,
  PROPOSED). ASCII keys only; `fps`
  is a rational and `duration_s` is normalized to an exact integer millisecond value; first-class collections are
  canonicalized before serialization. References gain an `index` and `(role, index)` is unique.
- `spec_hash` is replaced by `identity_hash` plus `task_fingerprint`. Reproduction needs the same
  `identity_hash` and seed; a different `task_fingerprint` is an inexact reproduction, recorded, not an error.
- No persisted data exists, so the bump from canonical v1 to v2 needs no migration. Golden vectors are
  cross-checked by two independent implementations in a later authorized session; none exist yet.

### 3. Approval invalidation (UD-3, UD-5, UD-6; design section D)
- A pure function (`classify_changes`, `stale_reasons`, a read-only invalidation matrix) decides which approvals
  go stale. The domain computes; plan apply and asset-state changes append the STALE transitions.
- Creative-identity changes (matrix rows 1-10, exactly the identity payload) make take reviews, slot selections
  and downstream gates stale. Execution parameters, including declared-adaptable width, height and duration,
  never make take reviews, version reviews or slot selections stale (UD-3 holds by construction).
- Model, template and engine changes are advisory for take reviews (I-1a): a flag (execution drift, not
  reproducible), not a stale approval. They do make an unused adaptation approval stale.
- A slot-definition change that leaves the bound version compatible keeps the selection; an incompatible one
  makes it stale (I-1c).
- An adaptation approval already consumed by a started take is judged as of the start of the job's first
  attempt (as-of evaluation); later staleness never retroactively invalidates a legitimately produced take.
- Approval REJECTED is terminal; at most one current (PENDING, APPROVED or REJECTED) approval exists per
  subject and purpose.
- Authority rules: an adapted take is always a new take with its adapted keys named. The pre-run adaptation
  approval may be `PRE_AUTHORIZED_POLICY` only when `identity_hash` equals the source's (execution-only
  change); otherwise it is human (UD-6). CONTENT_REVIEW is human-only for all takes for now (I-2, minimal
  policy catalogue: one execution-adaptation policy class per binding, nothing else).
- Shot state moves only on a creative-identity change (new edge APPROVED to READY); an execution-only change
  causes an advisory, no transition.

### 4. Recovery (UD-4, UD-5, UD-7 amended; design section C, alternative C-1)
**Policy and snapshot.**
- Limits are registry data, optional on the engine (default) and on the binding (override): maximum
  infrastructure retries, maximum resumes and maximum resume depth. A binding block replaces the engine block
  as a whole; fields are not merged. No domain default exists and no shipped binding gets a block now
  (I-8: per-binding limits unset).
- A Job stores a required `RecoveryPolicySnapshot` (source, the three limits, digest) at creation. A later
  registry change never alters a running job's budget. The domain never contains a literal default such as 2.
- R-9 (planner ruling under UD-7, PROPOSED) `max_resume_depth`, when set, must be >= 1; None means a depth of 1.

**Attempt causes.** An interrupted or failed attempt carries a cause: ENGINE, STUDIO_PROCESS, HOST_RESTART or
USER_STOP (FAILED implies ENGINE). Initiator is AUTOMATIC or HUMAN; a human re-run has origin MANUAL_RESTART
and is never budgeted. Counters are always derived from persisted attempt rows, never held in memory.

**Budget and breaker (UD-7 amended).**
| Cause | Consumes budget | Counts toward the breaker | Automatic follow-up |
|---|---|---|---|
| ENGINE (failed, or interrupted after start) | yes | no | per policy limit |
| STUDIO_PROCESS (interrupted) | no | yes | per policy (non-budget) |
| HOST_RESTART (interrupted) | no | yes | per policy (non-budget) |
| USER_STOP (interrupted) | no | no | never (R-2) |

ENGINE requires a started attempt.

**Planner rulings under UD-7 (R-1 to R-5 and R-8 to R-12; PROPOSED with this ADR, not user decisions).**
- R-1 The breaker counts an interrupt by its interruption time (`finished_at`, set when the interruption is
  marked), not by `started_at`.
- R-2 After USER_STOP there is never an automatic follow-up; only a human MANUAL_RESTART.
- R-3 An attempt interrupted before it started is not recovery: re-dispatch consumes no budget, needs no policy
  limit and never counts toward the breaker.
- R-11 (planner ruling under UD-7, PROPOSED) R-2 takes precedence over R-3. A re-dispatch after a never-started
  attempt is an AUTOMATIC attempt with origin INFRASTRUCTURE_RETRY. Attempt states that imply the engine ran
  require `started_at`. PENDING forbids started_at; RUNNING, INGESTING, SUCCEEDED and FAILED require it; INTERRUPTED and CANCELED may omit it; a launch failure (PENDING -> FAILED) stamps started_at when it fails.
- R-4 Breaker K and T have no code defaults. Their values live only in `config/studio.toml` (K = 3 interrupts
  within T = 1800 s, provisional: I-8). If the configuration is absent, the breaker fails closed. K and T fail
  closed independently: an absent K is K = 1, and an absent T means an unbounded window (every counted
  interrupt since the last reset). R-8 makes the wording "only in `config/studio.toml`" precise.
- R-8 (planner ruling under UD-7, PROPOSED; supersedes R-4's wording and keeps its substance) K and T may be
  set only in the base `studio.toml` (the file `config_dir()` selects). A value from an environment overlay
  file or a `STUDIO_*` environment variable is a settings error. Constructor keyword arguments remain
  allowed. Values are strict integers. There are no code defaults.
- R-5 A limit of None means that recovery kind is never automatic. A limit of 0 means no budget-consuming
  follow-ups, while non-budget follow-ups (after STUDIO_PROCESS or HOST_RESTART) remain allowed.

**Resume.** A RESUME attempt is valid only if the artifact was listed by an earlier attempt of the same job,
its SHA-256 matches the pinned hash, its stage key matches the job's task fingerprint and stage (the fingerprint
must be complete), it is available, and the chain of consecutive resumes is within the maximum resume depth.

**Breaker mechanics.** Append-only events (tripped, reset, quarantine lifted) in the global database. The
domain is clock-free (`now` is passed in). On trip the queue is paused and the registry binding of the attempt
that reached K is quarantined. Reset and quarantine lift are human actions; nothing resets automatically. R-10 (planner ruling under UD-7,
PROPOSED): events are ordered by an explicit sequence number (unique and strictly increasing, with `at`
non-decreasing), never by time alone.

**Startup marking.** A pure function selects attempts still in PENDING, RUNNING or INGESTING and marks them
INTERRUPTED with cause HOST_RESTART (started before the host boot) or STUDIO_PROCESS, recording the boot id.
INTERRUPTED is terminal, so a second run in the same boot finds nothing: marking is idempotent and applied in
one transaction. R-12 (planner ruling, PROPOSED): idempotency requires atomic persistence of one boot's
transitions (persistence milestone).

**Admission refusal route.** A job-level failure record (error class, refusal reason, detail) replaces a
synthetic attempt. A permanent refusal fails the job with error class RESOURCE. EXCEEDS_CAPACITY on a job with
a take permits resource adaptation (a new take, UD-5). UNKNOWN_REQUIREMENT and PROFILE_INCOMPLETE permit none;
the route is calibration (the direction chosen in I-6; its amendment of ADR-0008 D-1 stays PROPOSED in
[ADR-0009](0009-proposed-amendments-to-accepted-adrs.md)). A job without a take has no adaptation path (I-12):
a human re-plans it as a new job with `source_job_id`.

**Provisional values.** K, T, every recovery limit and every other threshold in this ADR are provisional until
calibrated. They live in configuration or registry data, never in the domain. The AST tripwire tests (no default budget,
domain imports) are tripwires, not proofs (R-12, planner ruling, PROPOSED).

## Consequences
- `spec_hash` is renamed and the canonical version becomes 2; no persisted data exists, so no migration.
- Approvals stay valid across execution, model, template, engine and policy changes; the user sees advisories.
- The domain gains slots, fingerprint, invalidation and breaker modules; `studio.domain` remains standard-library
  only and uses `hashlib` for digests.
- Until limits are set, nothing recovers automatically after an engine failure; a human re-runs.
- If this ADR is not accepted, the M0.3 text of ADR-0010 (including its shared budget of 2) conflicts with UD-4
  and UD-7 and must be reworked before implementation proceeds.

## Open items
- The DELIVERABLE slot owner and a frame-extraction operation (I-11, timeline milestone).
- Calibration of K, T and any per-binding limit (I-8).
- Whether the planner rulings R-1 to R-5 and R-8 to R-12 are confirmed as user decisions.
- The converter from registry policy to the domain snapshot belongs to the scheduler milestone.
