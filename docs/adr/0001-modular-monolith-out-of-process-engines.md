# ADR-0001: Modular monolith with out-of-process engines

Status: Accepted

## Context
Single-user local system, 6 GB VRAM, two existing engines with different Python environments (ComfyUI;
LTX streaming) and more expected (TTS, ASR, upscaling). Alternatives: ComfyUI-centric graphs (couples
everything to ComfyUI, would wrap or abandon the validated LTX runtime) and microservices with a broker
(operational overhead without benefit for one user).

## Decision
One Python orchestration process (`studio`) with clean internal modules (domain, persistence, registry,
engines, resources, scheduler, workflows, assets, qc, audio, post, plan, cli). Every GPU workload runs
outside it, as a service (ComfyUI), an ephemeral subprocess tree (LTX, workers) or a media tool (FFmpeg).
The engine contract is minimal (`capabilities`, `prepare`, `requirements`, `execute`); lifecycle differences
are optional mixins, not forced into one shape. No microservices, no message broker.

## Consequences
- Each engine keeps its own environment and CUDA context; crashes do not take down the studio.
- GPU arbitration is explicit and centralized (ADR-0007).
- Adding a backend means an adapter plus registry entries, not a rewrite.
- A GPU upgrade is a hardware-profile change.
- Cost: engine swaps (model reloads) must be mitigated by engine-affinity scheduling.
