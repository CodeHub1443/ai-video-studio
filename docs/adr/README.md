# Architecture Decision Records

| ADR | Title | Status |
|---|---|---|
| [0001](0001-modular-monolith-out-of-process-engines.md) | Modular monolith with out-of-process engines | Accepted |
| [0002](0002-externally-managed-comfyui.md) | Externally managed ComfyUI | Accepted |
| [0003](0003-ltx-via-unchanged-cli.md) | LTX integration through its unchanged CLI | Accepted |
| [0004](0004-global-sqlite-per-project-stores.md) | Global SQLite database and per-project stores | Accepted |
| [0005](0005-separate-torch-free-environment.md) | Separate torch-free Python environment | Accepted |
| [0006](0006-asset-ingestion-conditional-hardlinks.md) | Asset ingestion with conditional hardlinks | Accepted |
| [0007](0007-gpu-admission-device-level.md) | GPU admission measured at device level | Accepted (corrects Rev. 2 s6.3) |
| [0008](0008-configuration-and-registry-design.md) | Configuration and registry design | **Proposed: awaiting approval** |
| [0009](0009-proposed-amendments-to-accepted-adrs.md) | Proposed amendments to ADR-0003, 0006 and 0007 | **Proposed: awaiting approval** |
| [0010](0010-domain-model-and-state-definitions.md) | Domain model and state definitions | **Proposed: awaiting approval** |

Format: Context, Decision, Consequences. Accepted ADRs are immutable: changes to them are made only by a
superseding ADR or a proposed amendment (such as 0009) that is explicitly marked as awaiting approval until
accepted. Thresholds mentioned anywhere are provisional until calibrated.
