# Domain model (M0.3)

Code: `src/studio/domain/`. Standard library only; independent of ComfyUI, LTX, FFmpeg, pydantic, the
configuration and the registry. Data contracts and pure checks only: no persistence, execution, scheduling,
ingestion or file access. **Nothing here has been executed, type-checked, linted or tested** (A1).

## Modules
| Module | Contents |
|---|---|
| `ids.py` | id aliases (`ProjectId`, `JobId`, ...), lexical checks |
| `vocab.py` | `Operation`, `MediaType`, `CommercialUse` (mirror the registry's enums by value, no import) |
| `canonical.py` | canonical JSON rules, `freeze_params`, `canonical_bytes` |
| `states.py` | state enums, `TransitionTable`, all transition tables, `check_all_tables()` |
| `recovery.py` | the five recovery kinds, attempt/take origins, `ErrorClass`, rules |
| `assets.py` | `ObjectRef`, `Asset`, `AssetVersion`, `MediaMetadata`, `Provenance`, `RightsInfo`, `LineageEdge`, `AssetRef` |
| `production.py` | `Project`, `Scene`, `Shot`, `Take`, `CreativeSpec`, `ExecutionSpec`, `check_parameter_partition` |
| `workflow.py` | `TaskSpec`, `Job`, `Attempt`, `Approval`, `Gate`, `NodeRef`, `Dependency`, `WorkflowGraph` |
| `validation.py` | `DomainSnapshot`, `validate_snapshot`, `ensure_valid_snapshot` |
| `graph.py`, `errors.py` | topological order helper; `ValidationIssue` and error types |

All entities are frozen dataclasses with tuples for collections and read-only mappings for parameters.
Entities holding parameters are therefore not hashable; use their ids as keys.

## Entities and ownership
```
Project 1-* Scene 1-* Shot 1-* Take 1-1 Job 1-* Attempt
Shot has CreativeSpec (+ ExecutionSpec); Job has TaskSpec = CreativeSpec + ExecutionSpec
Asset 1-* AssetVersion 1-1 ObjectRef(sha256, size)      LineageEdge: version -> version (append-only)
Approval -> subject (take | asset version | gate);  Gate = DAG node; Dependency = edge between Job/Gate nodes
```
A take is executed by at most one job; a job may exist without a take (e.g. a transcode).

## Creative vs execution-only
`CreativeSpec` = operation, requested capability (`binding_id`), prompt, negative prompt, size, fps,
duration, **seed**, references (`AssetRef`) and free parameters. Only it feeds the future `spec_hash`.
`ExecutionSpec` = free parameters that change *how* it is computed. `check_parameter_partition(creative,
execution, binding.execution_only)` reports a key on both sides, an execution-only key among the creative
parameters, and an execution key the binding does not declare (it could silently escape the fingerprint).

## Canonical representation
`CreativeSpec.canonical_form(resolved=...)` returns a plain structure; `canonical_bytes` turns it into
UTF-8 JSON (sorted keys, no whitespace, type-preserving numbers, finite floats, 64-bit integers, strings
unnormalized; full rules in `canonical.py`, versioned by `CANONICAL_VERSION`). The *resolved* form requires a
seed, a capability and content-pinned references (`sha256`, not database ids) and raises
`UnresolvedSpecError` otherwise; the *authored* form (for plan diffs) is tagged and cannot collide with it.
`TaskSpec.fingerprint_input()` = resolved creative form only. **No hash function is chosen or invoked here.**

## State machines
Defined in `states.py`; terminal states have no successors. `JobState` is exactly the architecture's set; the
others are proposals (ADR-0010).

| Entity | States | Terminal |
|---|---|---|
| Project | draft, active, on_hold, completed, archived | archived |
| Shot | draft, ready, generating, in_review, approved, canceled | canceled |
| Take | planned, generating, produced, approved, rejected, failed, canceled | rejected, failed, canceled |
| Job | queued, blocked, ready, waiting_resources, running, ingesting, succeeded, retry_pending, failed, canceled, interrupted | succeeded, failed, canceled |
| Attempt | pending, running, ingesting, succeeded, failed, canceled, interrupted | succeeded, failed, canceled, interrupted |
| Approval | pending, approved, rejected, stale, canceled | stale, canceled |
| Gate | blocked, awaiting_decision, passed, rejected, stale, canceled | stale, canceled |
| AssetVersion | staged, quarantined, available, corrupt, retired | retired |

Job path: `queued -> ready -> waiting_resources -> running -> ingesting -> succeeded`; every job, CPU or GPU,
passes `waiting_resources` (admission, ADR-0008 D-1). `running`/`ingesting` may go to `retry_pending`
(then `ready`), `failed`, `interrupted` or `canceled`. Decided approvals are never edited; they only become
`stale`. An asset version's bytes never change in any state.

## Recovery: five kinds, kept apart
| Kind | Attempt | Job | Take | spec_hash | seed | Approval | Trigger |
|---|---|---|---|---|---|---|---|
| infrastructure retry | new, same job | no | no | same | same | no | `INFRASTRUCTURE` failure |
| resume (reuse stage-1 artifact) | new, same job | no | no | same | same | no | `INFRASTRUCTURE` failure |
| resource adaptation | new job | yes | new (proposal) | either | same | **yes** | `RESOURCE` failure |
| creative regeneration | new job | yes | new | different | either | no | a human, never an error |
| reproduction | new job | yes | new | same | same | no | a human |

`AttemptOrigin` and `TakeOrigin` are separate enums, so mixing levels is unrepresentable. A bad-looking output
is a *succeeded* attempt and a *rejected* take; no `ErrorClass` leads to creative regeneration. Retry budget
(`max_infrastructure_retries`, default 2, **provisional**) counts retry and resume attempts together.

## Validation
Entity constructors enforce field-level rules and raise `DomainValidationError`. `validate_snapshot` returns
every cross-entity finding as `ValidationIssue(code, path, message)`:
- unique ids; contiguous numbering (scene/shot ordinals, take numbers, version numbers, attempt numbers);
- references exist and belong to the project (scene/shot/take/job/attempt/approval/gate/asset/version/lineage ends);
- selected take belongs to the shot and is produced/approved; shot, take, job and attempt state coherence
  (e.g. approved shot needs approved selected take; a produced take needs a succeeded job whose attempts produced
  its outputs; running job needs a matching live attempt; at most one live attempt, and it is the latest);
- recovery rules: attempt retry/resume only after a failed/interrupted attempt whose error class permits it,
  within budget; reproduction keeps hash and seed; regeneration changes hash; adaptation keeps seed and carries an
  approved adaptation approval before leaving `planned`;
- asset rules: metadata plausible for the media type; produced versions name existing job/attempt; derived versions
  have lineage parents, imported ones none; lineage has no duplicates or cycles; pinned references match content hash;
- workflow: DAG nodes unique, dependencies reference existing nodes, no duplicates, acyclic; data dependencies
  connect jobs; gates match their decision record.

## Deliberately not here
Hashing, id generation, timestamps policy, database models, slots/slot bindings, scheduler readiness,
executors, ingestion, resource admission, QC, any license/approval/resource *policy*. Open items:
`docs/adr/0010-domain-model-and-state-definitions.md`.
