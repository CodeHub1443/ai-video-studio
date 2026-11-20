# ADR-0002: Externally managed ComfyUI

Status: Accepted

## Context
ComfyUI 0.45.0 runs as a persistent two-process tree (venv launcher + server owning 127.0.0.1:8188) started
by the user. Its `POST /free` only sets flags processed later, so release is a request, not a guarantee.
Upstream source, custom nodes and configuration must not be modified.

## Decision
The studio treats ComfyUI as an externally managed resident service. It may use GET endpoints, `POST /prompt`
(under a studio `client_id`), `POST /interrupt` (studio prompt ids only) and `POST /free`. It never starts,
stops, restarts or reconfigures ComfyUI and never edits its files. Release is verified by measurement
(ADR-0007); on timeout the job waits and the user is notified. A studio-managed mode is postponed and would
be an explicit opt-in.

## Consequences
- Simple, safe coexistence with the user's own ComfyUI sessions; foreign queue items put the GPU in
  `GPU_BLOCKED_EXTERNAL` rather than competing.
- The studio cannot force VRAM release; users who need more headroom can launch ComfyUI with lower-VRAM
  flags themselves.
- Workflow templates are API-format files owned by the studio and validated against `/object_info`.
