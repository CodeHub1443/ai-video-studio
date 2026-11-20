# Architecture (condensed, Revision 2)

Status: accepted direction. Thresholds are **provisional** until authorized calibration.

## 1. Shape
A modular monolith (`studio`, torch-free) owns the domain model, plan sync, DAG scheduler, resource
manager, executors, asset store, QC, review and post-production. All GPU work runs out of process:
- **ComfyUI**: persistent HTTP/WebSocket service, externally managed (ADR-0002).
- **LTX-2.5**: ephemeral subprocess tree via its unchanged `render.py` (ADR-0003).
- **Workers** (TTS, ASR, upscaling; later): ephemeral subprocesses in their own venvs.
- **FFmpeg/ffprobe**: media tools.

Interface is CLI-first. A local web UI and an optional LLM assistant come later; the core never imports them.

## 2. Layers and responsibilities
| Layer | Owns | Must not |
|---|---|---|
| Workflow recipes | Plan intent -> job/gate graph, idempotently | Execute, inspect GPU state |
| DAG scheduler | Job/gate states, readiness, priority, affinity, retry decisions | Read telemetry or engine internals |
| Resource manager | GPU state machine, RAM/commit budget, admission, leases | Know about shots or gates |
| Executors | Run one attempt under a lease, events, outputs, `error_class` | Decide retries |
| Asset ingest | Atomic, verified import; DB commit completing an attempt | Run engines |

Human approval gates never hold a lease. Independent branches proceed while gates are open.

## 3. Engine contract
Minimal common surface: `capabilities`, `prepare`, `requirements`, `execute`. Lifecycle is expressed by
optional mixins: `ResidentService` (ComfyUI), `EphemeralProcess` (LTX, workers), `MediaTool` (FFmpeg),
`Resumable`, `Previewable`. Task parameters are split into **creative** (fingerprinted into `spec_hash`)
and **execution-only** (not fingerprinted).

## 4. GPU lifecycle
States: `GPU_UNKNOWN`, `GPU_IDLE`, `GPU_RELEASING`, `GPU_RELEASED` (10 s verdict), `GPU_LEASED`,
`GPU_RETURNING`, `GPU_BLOCKED_EXTERNAL`, `GPU_FAULT`.

Signals measured separately: S1 device VRAM (NVML), S2 ComfyUI allocator reservation (`/system_stats`
`torch_vram_total`), S3 CUDA-context budget, S4 RAM and commit headroom, S5 GPU utilization (advisory),
S6 external activity. Under WDDM per-process VRAM is not reported, so accounting is device-level.

### 4.1 Admission formula (correction to section 6.3 of the original Revision 2 document)
The original Revision 2 document is not reproduced in this repository; this file is its condensed form.
Revision 2 wrote: `need_vram + sum(cuda_context of other live processes) + margin <= S1.free`.
That double-counts: NVML device free VRAM already excludes memory held by every live process,
including their CUDA contexts. Corrected rule:

```
admit(E, task)  iff  S1.free_min_over_window  >=  need_vram(E, task) + new_context_term + vram_margin
```
- **S1 (NVML device free VRAM) is the primary hard measurement.** It is the only ground truth for the whole device.
- **Other processes' contexts are not added.** They are already reflected in S1.
- **`new_context_term`** covers only the context of the process about to be started, and only if the
  registry's `need_vram` estimate for that engine does not already include it (flag `estimate_includes_context`).
  For the already-resident ComfyUI process the term is 0.
- **Uncertainty is handled conservatively** by `vram_margin` and the jitter check, not by summing guesses. An
  unknown or unmeasurable quantity is rounded against admission (wait or adapt), never toward it.
- **S2/S3 are not summed into the gate.** S2 decides whether ComfyUI has yielded memory (release condition);
  S3 is retained only as a calibration constant for `new_context_term` and for diagnostics.
- **All thresholds are provisional** (margin 0.3 GB, jitter 128 MB, window 5 x 1 s, context default 400 MB,
  etc.) until an explicitly authorized calibration milestone (M3.2) measures them.
- **Unknown requirements are never admissible** (user decision, ADR-0008 D-1; proposed amendment to
  ADR-0007 in ADR-0009). A binding whose VRAM, RAM or commit need is unknown (`null` in the registry)
  cannot pass static compatibility or admission; it is not assumed to be small.

## 5. Scheduler and retries
Job states: `QUEUED, BLOCKED, READY, WAITING_RESOURCES, RUNNING, INGESTING, SUCCEEDED, RETRY_PENDING,
FAILED, CANCELED, INTERRUPTED`. Gates are DAG nodes passed by humans or pre-authorized QC policy.
Retry taxonomy: infrastructure retry (same seed and creative params, same `spec_hash`, max 2), resume
(reuse a matching stage-1 artifact), resource adaptation (new job, needs approval; preview tier may be
pre-authorized), creative regeneration (new take), reproduction. The resolved task and seed are persisted
before the first attempt.

## 6. Assets
Per-project, content-addressed, immutable objects (SHA-256). Atomic staged ingest, then a single DB
transaction. Separate concepts: Object, Asset, AssetVersion, Slot, SlotBinding, LineageEdge, Approval.
See ADR-0006 for move/copy/hardlink rules (designed, not implemented; ADR-0009 proposes amending them
because the studio may not delete or move-source-delete in engine directories). `assets/` holds read-only
originals: they are only ever copied from. Access is limited by actor and operation
(docs/security-model.md).

## 7. Plan and database
YAML (`projects/<slug>/plan/`) is authored intent; SQLite (`data/studio.db`) is runtime history. Changes
go `plan diff` -> `plan apply --confirm <diffhash>`. Apply is append-only: it archives and marks approvals
stale, never deleting takes, assets, approvals or history.

## 8. Post-production
FFmpeg is the render engine via a timeline -> filtergraph compiler. NVENC/NVDEC/CUDA filters are real GPU
work (VRAM, encoder time, bandwidth) and require the lease; masters default to CPU encoders.

## 9. Not yet decided
Delivery format, audio sources and licenses (D7-D9; every model license is currently `unknown`), the
hardlink-or-copy choice (D6), import of protected perfume files (D11), and all resource requirements and
thresholds (provisional or unknown until M3.x calibration). The open items of M0.2 are listed in
`docs/adr/0008-configuration-and-registry-design.md`.
