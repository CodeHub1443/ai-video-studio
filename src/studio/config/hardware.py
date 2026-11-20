"""Hardware profile: GPU admission thresholds (ADR-0007).

Every threshold is PROVISIONAL until the authorized calibration milestone (M3.2). The profile is data;
nothing here queries NVML or any device. A profile can only be marked ``provisional = false`` together
with a ``calibration_ref`` naming the recorded calibration run, so calibrated status cannot be asserted
by accident.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from studio.config.paths import validate_segment
from studio.versions import SCHEMA_VERSION, SchemaVersion

PROFILE_NAME_PATTERN = r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$"
_PROFILE_NAME = re.compile(PROFILE_NAME_PATTERN)


def validate_profile_name(name: str) -> str:
    if not _PROFILE_NAME.match(name):
        raise ValueError(f"invalid hardware profile name {name!r}")
    validate_segment(name)  # reserved device names, 8.3 aliases, ...
    return name


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AdmissionThresholds(_Strict):
    """Provisional values from the original architecture Revision 2 section 6.3 as corrected by ADR-0007
    (the corrected rule is docs/architecture.md section 4.1). None of these is measured."""

    # S1 stability window and margin: admit iff S1.free_min >= need + new_context_term + vram_margin
    vram_margin_gb: float = Field(default=0.3, ge=0)
    vram_jitter_mb: int = Field(default=128, ge=0)
    window_samples: int = Field(default=5, ge=2)
    window_interval_s: float = Field(default=1.0, gt=0)
    # S3: calibration constant used only for new_context_term of a process about to start
    default_cuda_context_mb: int = Field(default=400, ge=0)
    # S2: ComfyUI has yielded when torch_vram_total <= this
    comfy_reserved_max_mb: int = Field(default=256, ge=0)
    # S4
    ram_reserve_gb: float = Field(default=4.0, ge=0)
    commit_reserve_gb: float = Field(default=2.0, ge=0)
    # timing
    release_timeout_s: float = Field(default=90.0, gt=0)
    released_validity_s: float = Field(default=10.0, gt=0)
    queue_poll_interval_s: float = Field(default=1.0, gt=0)
    # return conditions
    return_vram_tolerance_mb: int = Field(default=128, ge=0)
    return_vram_deadline_s: float = Field(default=30.0, gt=0)
    return_ram_deadline_s: float = Field(default=60.0, gt=0)
    # S5 advisory
    util_baseline_margin_pct: float = Field(default=30.0, ge=0)


class HardwareProfile(_Strict):
    schema_version: SchemaVersion = SCHEMA_VERSION
    name: str
    description: str = ""
    gpu_vram_total_gb: float = Field(
        gt=0, description="nominal device VRAM; NVML is authoritative later"
    )
    # Unknown (None) until stated by the owner or measured; never guessed. Used only for static
    # registry-to-hardware compatibility checks.
    system_ram_gb: float | None = Field(default=None, gt=0)
    commit_limit_gb: float | None = Field(default=None, gt=0)
    gpu_slots: int = Field(default=1, ge=1)
    allow_media_during_inference: bool = False
    provisional: bool = True  # True until M3.2 calibration has been authorized and recorded
    calibration_ref: str | None = Field(default=None, min_length=1)
    thresholds: AdmissionThresholds = Field(default_factory=AdmissionThresholds)

    @model_validator(mode="after")
    def _check(self) -> HardwareProfile:
        validate_profile_name(self.name)
        if self.thresholds.vram_margin_gb >= self.gpu_vram_total_gb:
            raise ValueError("vram_margin_gb must be smaller than gpu_vram_total_gb")
        if not self.provisional and self.calibration_ref is None:
            raise ValueError(
                "provisional=false requires calibration_ref (the recorded calibration run)"
            )
        if self.provisional and self.calibration_ref is not None:
            raise ValueError(
                "calibration_ref is set but provisional is still true; they must agree"
            )
        return self


def load_hardware_profile(config_dir: Path, name: str) -> HardwareProfile:
    """Read ``<config_dir>/hardware/<name>.toml``. Read-only; raises on missing or invalid file."""
    validate_profile_name(name)
    path = config_dir / "hardware" / f"{name}.toml"
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    profile = HardwareProfile.model_validate(data)
    if profile.name != name:
        raise ValueError(f"{path}: profile name {profile.name!r} does not match file name {name!r}")
    return profile
