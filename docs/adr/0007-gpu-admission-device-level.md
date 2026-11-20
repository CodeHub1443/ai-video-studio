# ADR-0007: GPU admission measured at device level

Status: Accepted. Corrects Architecture Revision 2 section 6.3.

## Context
Under WDDM, `nvidia-smi` reports per-process GPU memory as N/A, so only device-level VRAM can be measured.
Revision 2 defined admission as `need_vram + sum(cuda_context_mb of other live CUDA processes) + margin <=
S1.free`. NVML device free VRAM already excludes everything held by live processes, including their CUDA
contexts, so adding other processes' contexts counts the same memory twice and wrongly rejects feasible jobs.

## Decision
1. **NVML device-level free VRAM (S1) is the primary hard admission measurement**, evaluated as the minimum
   over a stability window, together with a jitter bound.
2. Admission rule: `S1.free_min >= need_vram(E, task) + new_context_term + vram_margin`.
3. **No double counting.** Contexts and allocations of processes already running are already reflected in S1
   and are never added. `new_context_term` covers only the process about to start and is 0 if the engine's
   `need_vram` estimate already includes its context (`estimate_includes_context`) or if the requester is the
   already-resident ComfyUI.
4. **Conservative handling of uncertainty.** Unknowns are covered by `vram_margin`, the jitter check and the
   stability window. When a quantity cannot be measured, the decision errs toward waiting or adapting.
5. S2 (ComfyUI's reserved memory) is a release condition (has ComfyUI yielded?), not an additive term. S3
   (context estimate) is a calibration constant for `new_context_term` and a diagnostic. S5 (utilization) is
   advisory only.
6. **All thresholds are provisional**: margin 0.3 GB, jitter 128 MB, 5 samples at 1 s, default context 400 MB,
   release limits such as `comfy_reserved_max_mb` 256, RAM/commit reserves, and timeouts. They stay
   provisional until an explicitly authorized calibration milestone (M3.2, authorization A5/A6/A7).

## Consequences
- Admission is simpler and measurable; fewer false rejections.
- Per-process attribution of leaks remains impossible under WDDM; failures are diagnosed against the
  pre-lease snapshot, and the studio-owned process tree is terminated if return conditions fail.
- Admission quality depends on the margin until calibration; early runs may be conservative.
