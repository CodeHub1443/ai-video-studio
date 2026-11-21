"""The five ways to "try again", kept apart (docs/architecture.md section 5), and the recovery policy (UD-7).

The central rule: **what repeats, and at which level, differs per kind.**

| Kind                  | Attempt  | Job | Take | identity  | seed   | Approval | Trigger         |
|-----------------------|----------|-----|------|-----------|--------|----------|-----------------|
| INFRASTRUCTURE_RETRY  | same job | no  | no   | same      | same   | no       | INFRASTRUCTURE  |
| RESUME                | same job | no  | no   | same      | same   | no       | INFRASTRUCTURE  |
| RESOURCE_ADAPTATION   | new job  | yes | yes  | either    | same   | **yes**  | RESOURCE        |
| CREATIVE_REGENERATION | new job  | yes | yes  | different | either | no       | a human         |
| REPRODUCTION          | new job  | yes | yes  | same      | same   | no       | a human         |

Resource adaptation always creates a new take, linked to its source (UD-5). A human re-run of an attempt is
``AttemptOrigin.MANUAL_RESTART``: it is never budgeted and never automatic.

Consequences encoded below:
* Attempt-level recovery (``AttemptOrigin``) and take-level recovery (``TakeOrigin``) are different enums, so
  an infrastructure retry can never be recorded as a regeneration or vice versa.
* A creative problem (a bad-looking output) is never an attempt error: it ends in a *successful* attempt and a
  rejected take. ``ErrorClass`` has no creative member, and no error class permits CREATIVE_REGENERATION.

Recovery policy (UD-7, amended)
* The limits come from a policy snapshot stored on the Job (``RecoveryPolicySnapshot``). There is **no default**
  and no shared budget: retries and resumes have separate limits, and a limit that is not set means "not allowed".
* A limit of ``None`` means that kind of recovery is **never automatic**. A limit of ``0`` means no
  budget-consuming follow-ups; follow-ups after a studio or host interruption consume no budget and therefore
  remain allowed.
* Engine-side failures consume the budget. ``STUDIO_PROCESS`` and ``HOST_RESTART`` interruptions consume none but
  count toward the crash-loop breaker. ``USER_STOP`` consumes none, does not count toward the breaker, and is
  never followed by an automatic retry or resume, whatever the policy: only a human ``MANUAL_RESTART``.
* An attempt interrupted before it started is not a recovery event: an automatic re-dispatch of the job
  consumes no budget, needs no policy limit and never counts toward the breaker. That re-dispatch is an
  AUTOMATIC attempt with origin ``INFRASTRUCTURE_RETRY`` (there is no separate origin for it). R-2 outranks
  this rule: after a ``USER_STOP`` even a never-started attempt is followed only by a human ``MANUAL_RESTART``.
* Counters are always derived from persisted attempt rows (``retries_used``, ``resumes_used``), never kept.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from studio.domain.canonical import SAFE_INT_MAX
from studio.domain.errors import fail
from studio.domain.fingerprint import Fingerprint, FingerprintKind, compute_fingerprint
from studio.domain.ids import check_registry_id, check_text
from studio.domain.states import AttemptState, JobState

if TYPE_CHECKING:
    from studio.domain.workflow import Attempt, Job


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
    RESUME = "resume"  # reuses a matching stage artifact
    MANUAL_RESTART = "manual_restart"  # a human re-runs the job; never budgeted, never automatic


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


class AttemptEndCause(StrEnum):
    """Who or what ended a failed or interrupted attempt. Only ``ENGINE`` consumes the recovery budget."""

    ENGINE = "engine"  # the engine side failed or died
    STUDIO_PROCESS = "studio_process"  # the studio process vanished
    HOST_RESTART = "host_restart"  # the machine restarted
    USER_STOP = "user_stop"  # a human stopped it


class AttemptInitiator(StrEnum):
    AUTOMATIC = "automatic"
    HUMAN = "human"


class AdmissionRefusal(StrEnum):
    """Why admission permanently refused a job (it never became an attempt)."""

    EXCEEDS_CAPACITY = "exceeds_capacity"  # the requirement is known and larger than the machine
    UNKNOWN_REQUIREMENT = "unknown_requirement"  # a hard-gate requirement is unknown
    PROFILE_INCOMPLETE = "profile_incomplete"  # the hardware profile lacks what admission needs


class BudgetKind(StrEnum):
    RETRY = "retry"
    RESUME = "resume"


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
    identity: SpecRelation  # relation to the source's identity_hash
    seed: SpecRelation
    requires_approval: bool
    error_triggered: bool  # can follow from an attempt failure (otherwise a human initiates it)
    budget: BudgetKind | None  # which separate limit an automatic follow-up draws on, if any


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
            BudgetKind.RETRY,
        ),
        RecoveryKind.RESUME: RecoveryRule(
            RecoveryKind.RESUME,
            True,
            False,
            False,
            SpecRelation.SAME,
            SpecRelation.SAME,
            False,
            True,
            BudgetKind.RESUME,
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
            None,
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
            None,
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
            None,
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
        AttemptOrigin.MANUAL_RESTART: None,
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

# The budget an attempt origin draws on (the automatic attempt-level kinds only).
ORIGIN_BUDGET: Mapping[AttemptOrigin, BudgetKind | None] = MappingProxyType(
    {
        AttemptOrigin.INITIAL: None,
        AttemptOrigin.INFRASTRUCTURE_RETRY: BudgetKind.RETRY,
        AttemptOrigin.RESUME: BudgetKind.RESUME,
        AttemptOrigin.MANUAL_RESTART: None,
    }
)


def is_recovery_permitted(
    attempt_state: AttemptState, error_class: ErrorClass | None, kind: RecoveryKind
) -> bool:
    """May ``kind`` follow an attempt that ended in ``attempt_state`` with ``error_class``?

    Only FAILED and INTERRUPTED attempts can be followed by error-triggered recovery. An INTERRUPTED attempt
    without a class (the process vanished) is treated as INFRASTRUCTURE. Human-initiated kinds are never
    "permitted by an error" and return False here; they are checked against the source take instead. This is
    the *class* rule only: whether the policy limits allow the follow-up is a separate question.
    """
    if not RECOVERY_RULES[kind].error_triggered:
        return False
    if attempt_state is AttemptState.INTERRUPTED and error_class is None:
        error_class = ErrorClass.INFRASTRUCTURE
    if attempt_state not in (AttemptState.FAILED, AttemptState.INTERRUPTED) or error_class is None:
        return False
    return kind in PERMITTED_RECOVERY[error_class]


# ---------------------------------------------------------------- the policy snapshot


def _check_limit(value: object, path: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= SAFE_INT_MAX:
        fail("policy.limit", path, "a limit is an integer >= 0, or None for 'not allowed'")
    return value


@dataclass(frozen=True, kw_only=True)
class RecoveryPolicySnapshot:
    """The recovery limits in force for a job, copied onto it at creation (a later registry change never
    alters a running job). Every field is required: there is no default limit (UD-4, UD-7).

    ``source`` is ``"none"`` (no policy found: every limit ``None``), ``"binding:<registry id>"`` or
    ``"engine:<registry id>"``. A limit of ``None`` means that kind is never automatic; ``0`` allows no
    budget-consuming follow-up. ``max_resume_depth``, when set, is at least 1 (``0`` is rejected: a resume chain
    is at least one resume long, and ``None`` already means depth 1). ``digest`` fingerprints the snapshot (the
    "policy version").
    """

    source: str
    max_infrastructure_retries: int | None
    max_resumes: int | None
    max_resume_depth: int | None
    digest: Fingerprint = field(init=False, repr=False)

    def __post_init__(self) -> None:
        check_text(self.source, "recovery_policy.source")
        if self.source != "none":
            prefix, _, identifier = self.source.partition(":")
            if prefix not in ("binding", "engine"):
                fail("policy.source", "recovery_policy.source", "none, binding:<id> or engine:<id>")
            check_registry_id(identifier, "recovery_policy.source")
        limits = (
            _check_limit(
                self.max_infrastructure_retries, "recovery_policy.max_infrastructure_retries"
            ),
            _check_limit(self.max_resumes, "recovery_policy.max_resumes"),
            _check_limit(self.max_resume_depth, "recovery_policy.max_resume_depth"),
        )
        if self.max_resume_depth is not None and self.max_resume_depth < 1:
            fail(
                "policy.resume_depth",
                "recovery_policy.max_resume_depth",
                "a resume depth is at least 1, or None (which means depth 1)",
            )
        if self.source == "none" and any(limit is not None for limit in limits):
            fail("policy.source_none", "recovery_policy", "source 'none' carries no limits")
        object.__setattr__(
            self,
            "digest",
            compute_fingerprint(FingerprintKind.RECOVERY_POLICY, self.payload()),
        )

    @classmethod
    def no_automatic_recovery(cls) -> RecoveryPolicySnapshot:
        """The explicit "nothing configured" policy: no automatic retry and no automatic resume."""
        return cls(
            source="none",
            max_infrastructure_retries=None,
            max_resumes=None,
            max_resume_depth=None,
        )

    def payload(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "max_infrastructure_retries": self.max_infrastructure_retries,
            "max_resumes": self.max_resumes,
            "max_resume_depth": self.max_resume_depth,
        }

    def limit_for(self, budget: BudgetKind) -> int | None:
        if budget is BudgetKind.RETRY:
            return self.max_infrastructure_retries
        return self.max_resumes

    def permits(self, origin: AttemptOrigin) -> bool:
        """May an *automatic* attempt of this origin exist at all? ``None`` means never automatic."""
        budget = ORIGIN_BUDGET[origin]
        return budget is not None and self.limit_for(budget) is not None


@dataclass(frozen=True, kw_only=True)
class JobFailure:
    """Why a job failed without a failing attempt (admission refusal, invalid spec before any attempt)."""

    error_class: ErrorClass
    detail: str
    refusal: AdmissionRefusal | None = None

    def __post_init__(self) -> None:
        check_text(self.detail, "failure.detail")
        if self.refusal is not None and self.error_class is not ErrorClass.RESOURCE:
            fail("failure.refusal", "failure.refusal", "an admission refusal is a RESOURCE failure")


# ---------------------------------------------------------------- derived facts (pure, from attempt rows)


def never_started(attempt: Attempt) -> bool:
    """Interrupted before it started: not a recovery event (no budget, no breaker count)."""
    return attempt.state is AttemptState.INTERRUPTED and attempt.started_at is None


def consumes_budget(attempt: Attempt) -> bool:
    """Engine-side failure or interruption of an attempt that actually ran. Studio, host and user
    interruptions, and attempts that never started, consume nothing."""
    return (
        attempt.state in (AttemptState.FAILED, AttemptState.INTERRUPTED)
        and attempt.cause is AttemptEndCause.ENGINE
        and not never_started(attempt)
    )


def counts_toward_breaker(attempt: Attempt) -> bool:
    """A studio or host interruption of an attempt that had started. ``USER_STOP`` never counts, and neither
    does an attempt that never started."""
    return (
        attempt.state is AttemptState.INTERRUPTED
        and attempt.cause in (AttemptEndCause.STUDIO_PROCESS, AttemptEndCause.HOST_RESTART)
        and attempt.started_at is not None
    )


def _used(attempts: Iterable[Attempt], origin: AttemptOrigin) -> int:
    rows = list(attempts)
    by_id = {a.attempt_id: a for a in rows}
    count = 0
    for attempt in rows:
        if attempt.origin is not origin or attempt.previous_attempt_id is None:
            continue
        previous = by_id.get(attempt.previous_attempt_id)
        if previous is not None and consumes_budget(previous):
            count += 1
    return count


def retries_used(attempts: Iterable[Attempt]) -> int:
    """Automatic retries that followed a budget-consuming attempt."""
    return _used(attempts, AttemptOrigin.INFRASTRUCTURE_RETRY)


def resumes_used(attempts: Iterable[Attempt]) -> int:
    """Automatic resumes that followed a budget-consuming attempt."""
    return _used(attempts, AttemptOrigin.RESUME)


def resume_chain_depth(attempt: Attempt, attempts: Iterable[Attempt]) -> int:
    """Number of consecutive RESUME attempts ending at ``attempt`` (0 if it is not a resume)."""
    by_id = {a.attempt_id: a for a in attempts}
    depth = 0
    seen: set[str] = set()
    current: Attempt | None = attempt
    while (
        current is not None
        and current.origin is AttemptOrigin.RESUME
        and current.attempt_id not in seen
    ):
        seen.add(current.attempt_id)
        depth += 1
        previous_id = current.previous_attempt_id
        current = None if previous_id is None else by_id.get(previous_id)
    return depth


def permitted_take_recovery(job: Job) -> frozenset[RecoveryKind]:
    """Take-level recovery a failed job leaves open (UD-5, I-12).

    Only a job that has a take, failed at admission with ``EXCEEDS_CAPACITY`` and carries that failure record
    may be adapted: the result is always a *new* take that still needs approval. An unknown requirement or an
    incomplete profile cannot be fixed by adapting; a job without a take has no adaptation path (a human
    re-plans it as a new job). Attempt-level resource failures are classified by ``is_recovery_permitted``.
    """
    failure = job.failure
    if job.take_id is None or job.state is not JobState.FAILED or failure is None:
        return frozenset()
    if (
        failure.error_class is ErrorClass.RESOURCE
        and failure.refusal is AdmissionRefusal.EXCEEDS_CAPACITY
    ):
        return frozenset({RecoveryKind.RESOURCE_ADAPTATION})
    return frozenset()
