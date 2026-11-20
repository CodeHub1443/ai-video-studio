"""The five ways to "try again", kept apart (docs/architecture.md section 5).

The central rule: **what repeats, and at which level, differs per kind.**

| Kind                  | Attempt  | Job | Take | spec_hash | seed   | Approval | Trigger         |
|-----------------------|----------|-----|------|-----------|--------|----------|-----------------|
| INFRASTRUCTURE_RETRY  | same job | no  | no   | same      | same   | no       | INFRASTRUCTURE  |
| RESUME                | same job | no  | no   | same      | same   | no       | INFRASTRUCTURE  |
| RESOURCE_ADAPTATION   | new job  | yes | yes* | either    | same   | **yes**  | RESOURCE        |
| CREATIVE_REGENERATION | new job  | yes | yes  | different | either | no       | a human         |
| REPRODUCTION          | new job  | yes | yes  | same      | same   | no       | a human         |

(*) Whether an adapted result is a *new take* or a replacement of the same take is not decided (ADR-0010,
U-2). This module encodes the proposal "always a new take, linked to its source".

Consequences encoded below:
* Attempt-level recovery (``AttemptOrigin``) and take-level recovery (``TakeOrigin``) are different enums, so
  an infrastructure retry can never be recorded as a regeneration or vice versa.
* A creative problem (a bad-looking output) is never an attempt error: it ends in a *successful* attempt and a
  rejected take. ``ErrorClass`` has no creative member, and no error class permits CREATIVE_REGENERATION.
* The retry budget (``DEFAULT_MAX_INFRASTRUCTURE_RETRIES``) is provisional and counts only attempt-level recovery.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from studio.domain.states import AttemptState

# Provisional: "max 2" in docs/architecture.md section 5; not calibrated, not approved as final policy.
DEFAULT_MAX_INFRASTRUCTURE_RETRIES = 2


class RecoveryKind(StrEnum):
    INFRASTRUCTURE_RETRY = "infrastructure_retry"
    RESUME = "resume"
    RESOURCE_ADAPTATION = "resource_adaptation"
    CREATIVE_REGENERATION = "creative_regeneration"
    REPRODUCTION = "reproduction"


class AttemptOrigin(StrEnum):
    """Why an attempt exists. Only attempt-level recovery kinds appear here."""

    INITIAL = "initial"
    INFRASTRUCTURE_RETRY = "infrastructure_retry"
    RESUME = "resume"  # reuses a matching stage-1 artifact


class TakeOrigin(StrEnum):
    """Why a take exists. Only take-level recovery kinds appear here."""

    INITIAL = "initial"
    CREATIVE_REGENERATION = "creative_regeneration"
    RESOURCE_ADAPTATION = "resource_adaptation"
    REPRODUCTION = "reproduction"


class ErrorClass(StrEnum):
    """Classification an executor attaches to a failed attempt. Executors classify; they never decide retries.

    Proposed vocabulary (ADR-0010 P-4): the architecture names ``error_class`` but not its members.
    """

    INFRASTRUCTURE = (
        "infrastructure"  # engine/process/network flake, transient; same inputs may succeed
    )
    RESOURCE = (
        "resource"  # not enough VRAM/RAM/commit/time; the same task will not fit as configured
    )
    INVALID_SPEC = "invalid_spec"  # the task is wrong or unsupported by the binding
    INTEGRITY = "integrity"  # ingest verification failed (hash, probe, size)
    UNKNOWN = "unknown"  # cannot be classified; never retried automatically


class SpecRelation(StrEnum):
    SAME = "same"
    DIFFERENT = "different"
    EITHER = "either"


@dataclass(frozen=True)
class RecoveryRule:
    kind: RecoveryKind
    new_attempt: bool  # an additional attempt row (in the same job, or the first of a new job)
    new_job: bool
    new_take: bool
    spec_hash: SpecRelation  # relation to the source's spec_hash
    seed: SpecRelation
    requires_approval: bool
    error_triggered: bool  # can follow from an attempt failure (otherwise a human initiates it)
    counts_against_retry_budget: bool


RECOVERY_RULES: Mapping[RecoveryKind, RecoveryRule] = MappingProxyType(
    {
        RecoveryKind.INFRASTRUCTURE_RETRY: RecoveryRule(
            RecoveryKind.INFRASTRUCTURE_RETRY,
            True,
            False,
            False,
            SpecRelation.SAME,
            SpecRelation.SAME,
            False,
            True,
            True,
        ),
        # Counted against the budget conservatively; whether resume has its own budget is open (U-3).
        RecoveryKind.RESUME: RecoveryRule(
            RecoveryKind.RESUME,
            True,
            False,
            False,
            SpecRelation.SAME,
            SpecRelation.SAME,
            False,
            True,
            True,
        ),
        RecoveryKind.RESOURCE_ADAPTATION: RecoveryRule(
            RecoveryKind.RESOURCE_ADAPTATION,
            True,
            True,
            True,
            SpecRelation.EITHER,
            SpecRelation.SAME,
            True,
            True,
            False,
        ),
        RecoveryKind.CREATIVE_REGENERATION: RecoveryRule(
            RecoveryKind.CREATIVE_REGENERATION,
            True,
            True,
            True,
            SpecRelation.DIFFERENT,
            SpecRelation.EITHER,
            False,
            False,
            False,
        ),
        RecoveryKind.REPRODUCTION: RecoveryRule(
            RecoveryKind.REPRODUCTION,
            True,
            True,
            True,
            SpecRelation.SAME,
            SpecRelation.SAME,
            False,
            False,
            False,
        ),
    }
)

# Which recovery an executor-classified failure may lead to. Deliberately has no route to
# CREATIVE_REGENERATION or REPRODUCTION: those are never automatic.
PERMITTED_RECOVERY: Mapping[ErrorClass, frozenset[RecoveryKind]] = MappingProxyType(
    {
        ErrorClass.INFRASTRUCTURE: frozenset(
            {RecoveryKind.INFRASTRUCTURE_RETRY, RecoveryKind.RESUME}
        ),
        ErrorClass.RESOURCE: frozenset({RecoveryKind.RESOURCE_ADAPTATION}),
        ErrorClass.INVALID_SPEC: frozenset(),
        ErrorClass.INTEGRITY: frozenset(),
        ErrorClass.UNKNOWN: frozenset(),
    }
)

ATTEMPT_ORIGIN_KIND: Mapping[AttemptOrigin, RecoveryKind | None] = MappingProxyType(
    {
        AttemptOrigin.INITIAL: None,
        AttemptOrigin.INFRASTRUCTURE_RETRY: RecoveryKind.INFRASTRUCTURE_RETRY,
        AttemptOrigin.RESUME: RecoveryKind.RESUME,
    }
)

TAKE_ORIGIN_KIND: Mapping[TakeOrigin, RecoveryKind | None] = MappingProxyType(
    {
        TakeOrigin.INITIAL: None,
        TakeOrigin.CREATIVE_REGENERATION: RecoveryKind.CREATIVE_REGENERATION,
        TakeOrigin.RESOURCE_ADAPTATION: RecoveryKind.RESOURCE_ADAPTATION,
        TakeOrigin.REPRODUCTION: RecoveryKind.REPRODUCTION,
    }
)


def is_recovery_permitted(
    attempt_state: AttemptState, error_class: ErrorClass | None, kind: RecoveryKind
) -> bool:
    """May ``kind`` follow an attempt that ended in ``attempt_state`` with ``error_class``?

    Only FAILED and INTERRUPTED attempts can be followed by error-triggered recovery. An INTERRUPTED attempt
    without a class (the process vanished) is treated as INFRASTRUCTURE. Human-initiated kinds are never
    "permitted by an error" and return False here; they are checked against the source take instead.
    """
    if not RECOVERY_RULES[kind].error_triggered:
        return False
    if attempt_state is AttemptState.INTERRUPTED and error_class is None:
        error_class = ErrorClass.INFRASTRUCTURE
    if attempt_state not in (AttemptState.FAILED, AttemptState.INTERRUPTED) or error_class is None:
        return False
    return kind in PERMITTED_RECOVERY[error_class]
