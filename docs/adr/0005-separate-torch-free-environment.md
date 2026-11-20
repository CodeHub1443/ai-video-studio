# ADR-0005: Separate torch-free Python environment

Status: Accepted

## Context
ComfyUI and LTX each have their own venv (both torch 2.14.1+cu130, with different dependency sets). Sharing
an environment risks dependency conflicts and would drag GPU libraries into the orchestrator.

## Decision
The studio uses its own Python 3.12 environment that does not depend on torch or any GPU inference
library. `pyproject.toml` declares ranges only; no resolved lockfile is committed at this stage, since
resolving needs environment setup and network access (authorization A2). Tooling choice (e.g. uv) is
finalized when the environment is authorized. Engines and workers keep and use their own environments.
GPU telemetry uses nvidia-ml-py (NVML) and psutil.

## Consequences
- Fast, light orchestrator; engine upgrades cannot break it.
- Worker scripts run inside their target environments and communicate over JSON lines on stdio.
- Dependency ranges may be tightened once a lockfile exists.
