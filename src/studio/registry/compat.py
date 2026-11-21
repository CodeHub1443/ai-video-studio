"""Static registry-to-hardware compatibility check (pure given the profile file; no NVML, no devices).

This is NOT runtime admission (ADR-0007): that decision uses live NVML/RAM readings and belongs to the
resource manager. Here "statically compatible" only means "could ever fit, from declared numbers".

Approved rules:

* **Unknown never passes.** A binding is statically compatible only if EVERY hard-gate requirement it declares is
  known: VRAM (GPU classes ``exclusive``/``media``; a ``none`` binding declares no VRAM need), RAM and
  commit headroom (all bindings, CPU and GPU alike). Any unknown requirement blocks it.
* **The profile is not injectable.** The hardware profile is loaded here, from the profile name selected
  in ``StudioSettings``. Callers pass settings, never a profile object, so a different profile cannot be
  substituted at this boundary.
* The profile must itself state what the comparison needs: unknown ``system_ram_gb`` or
  ``commit_limit_gb`` blocks any binding that has to be compared against it.

This answers "could the binding ever fit this machine?" from declared numbers. It is not the runtime
decision (ADR-0007), which uses live NVML/RAM readings. ``est_minutes`` is not a hard gate. Nominal VRAM
total, ``gpu_slots`` and ``allow_media_during_inference`` are not modelled here (open item U-7).
"""

from __future__ import annotations

from dataclasses import dataclass

from studio.config.hardware import HardwareProfile, load_hardware_profile
from studio.config.settings import StudioSettings
from studio.registry.loader import Registry, RegistryIssue, Severity
from studio.registry.schema import BindingSpec, EngineKind, GpuClass


@dataclass(frozen=True)
class BindingVerdict:
    binding_id: str
    profile_name: str
    statically_compatible: bool
    required_vram_gb: (
        float | None
    )  # new_context_term and margin included; None when not applicable/unknown
    codes: tuple[str, ...]  # every blocking reason


def admission_profile(settings: StudioSettings) -> HardwareProfile:
    """The one profile used for admission: the one ``settings.hardware_profile`` names. Read-only."""
    profile = load_hardware_profile(settings.config_dir, settings.hardware_profile)
    if (
        profile.name != settings.hardware_profile
    ):  # load_hardware_profile already checks; keep explicit
        raise ValueError("hardware profile does not match the selected profile name")
    return profile


def _evaluate(
    binding: BindingSpec, engine_kind: EngineKind | None, profile: HardwareProfile
) -> BindingVerdict:
    r, t = binding.resources, profile.thresholds
    blocking: list[str] = []
    required: float | None = None

    # VRAM: a hard gate for GPU classes only.
    if r.gpu is GpuClass.NONE:
        required = 0.0
    elif r.vram_gb.value is None:
        blocking.append("vram_unknown")
    else:
        resident = engine_kind is EngineKind.SERVICE
        context_gb = (
            0.0
            if (resident or r.estimate_includes_context is True)
            else (t.default_cuda_context_mb / 1024)
        )
        required = r.vram_gb.value + context_gb + t.vram_margin_gb
        if required > profile.gpu_vram_total_gb:
            blocking.append("exceeds_total_vram")

    # RAM and commit headroom: hard gates for every workload.
    for label, qty, limit, reserve in (
        ("ram", r.ram_gb, profile.system_ram_gb, t.ram_reserve_gb),
        ("commit", r.commit_gb, profile.commit_limit_gb, t.commit_reserve_gb),
    ):
        if qty.value is None:
            blocking.append(f"{label}_unknown")
        elif limit is None:
            blocking.append(f"profile_{label}_unknown")
        elif qty.value + reserve > limit:
            blocking.append(f"exceeds_{label}")

    return BindingVerdict(
        binding_id=binding.id,
        profile_name=profile.name,
        statically_compatible=not blocking,
        required_vram_gb=required,
        codes=tuple(blocking),
    )


def evaluate_registry(registry: Registry, settings: StudioSettings) -> dict[str, BindingVerdict]:
    profile = admission_profile(settings)
    out: dict[str, BindingVerdict] = {}
    for b in registry.bindings.values():
        engine = registry.engines.get(b.engine)
        out[b.id] = _evaluate(b, engine.kind if engine else None, profile)
    return out


def check_static_compatibility(
    registry: Registry, binding_id: str, settings: StudioSettings
) -> BindingVerdict:
    """Verdict for one binding. Unknown binding ids raise ``KeyError`` (never 'statically compatible')."""
    binding = registry.bindings[binding_id]
    engine = registry.engines.get(binding.engine)
    return _evaluate(binding, engine.kind if engine else None, admission_profile(settings))


def check_hardware_compatibility(
    registry: Registry, settings: StudioSettings
) -> list[RegistryIssue]:
    """One issue per blocking reason of every binding that is not statically compatible, plus a warning while the profile's
    thresholds are provisional. All blocking reasons are ``error``: nothing unknown is let through."""
    profile = admission_profile(settings)
    issues: list[RegistryIssue] = []
    if profile.provisional:
        issues.append(
            RegistryIssue(
                "warning",
                "thresholds_provisional",
                profile.name,
                "hardware thresholds are provisional (not calibrated; calibration is M3.2)",
            )
        )
    for v in evaluate_registry(registry, settings).values():
        for code in v.codes:
            severity: Severity = "error"
            issues.append(
                RegistryIssue(
                    severity, code, v.binding_id, f"not statically compatible on {v.profile_name!r}"
                )
            )
    return issues
