"""Crash-loop breaker (UD-7 amended) and idempotent startup interruption marking."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from studio.domain.breaker import (
    FAIL_CLOSED_K,
    AttemptInterruption,
    BreakerEvent,
    BreakerEventKind,
    JobTransition,
    apply_attempt_interruption,
    apply_job_transition,
    breaker_verdict,
    startup_interruptions,
)
from studio.domain.errors import DomainValidationError, InvalidTransition
from studio.domain.fingerprint import Fingerprint, FingerprintKind
from studio.domain.ids import AttemptId, JobId
from studio.domain.production import CreativeSpec
from studio.domain.recovery import (
    AttemptEndCause,
    AttemptInitiator,
    AttemptOrigin,
    ErrorClass,
    RecoveryPolicySnapshot,
    consumes_budget,
    counts_toward_breaker,
    retries_used,
)
from studio.domain.states import AttemptState, JobState
from studio.domain.vocab import Operation
from studio.domain.workflow import Attempt, Job, TaskSpec

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
WINDOW = 1800  # 30 minutes, an arbitrary test value (the domain has no default)
ENGINE, STUDIO, HOST, USER = (
    AttemptEndCause.ENGINE,
    AttemptEndCause.STUDIO_PROCESS,
    AttemptEndCause.HOST_RESTART,
    AttemptEndCause.USER_STOP,
)
IDENT = Fingerprint(kind=FingerprintKind.CREATIVE_IDENTITY, digest="aa" * 32)
TASKFP = Fingerprint(kind=FingerprintKind.TASK_FINGERPRINT, digest="bb" * 32)


def _code(excinfo: pytest.ExceptionInfo[DomainValidationError]) -> str:
    return excinfo.value.issues[0].code


def interrupt(
    n: int,
    cause: AttemptEndCause = STUDIO,
    *,
    started_ago: timedelta | None = timedelta(hours=1),
    interrupted_ago: timedelta = timedelta(minutes=1),
) -> Attempt:
    """An attempt of its own job, interrupted ``interrupted_ago`` before NOW (started ``started_ago``)."""
    kwargs: dict[str, Any] = {
        "attempt_id": f"att_{n}",
        "job_id": f"job_{n}",
        "number": 1,
        "initiated_by": AttemptInitiator.AUTOMATIC,
        "state": AttemptState.INTERRUPTED,
        "cause": cause,
        "finished_at": NOW - interrupted_ago,
    }
    if started_ago is not None:
        kwargs["started_at"] = NOW - started_ago
    return Attempt(**kwargs)


def verdict(interrupts: list[Attempt], events: list[BreakerEvent] | None = None, **kw: Any) -> Any:
    args: dict[str, Any] = {"k": 3, "window_s": WINDOW, "now": NOW}
    args.update(kw)
    return breaker_verdict(interrupts, events or [], **args)


def event(n: int, kind: BreakerEventKind, at: datetime, **kw: Any) -> BreakerEvent:
    base: dict[str, Any] = {"event_id": f"ev_{n}", "kind": kind, "at": at}
    if kind is BreakerEventKind.TRIPPED:
        base.update(binding_id="cap.t2i", attempt_ids=("att_1",), k=3, window_s=WINDOW)
    else:
        base["by_name"] = "ada"
        if kind is BreakerEventKind.QUARANTINE_LIFTED:
            base["binding_id"] = "cap.t2i"
    base.update(kw)
    return BreakerEvent(**base)


# ---------------------------------------------------------------- counting and the window


def test_trips_at_k_within_the_window() -> None:
    three = [interrupt(i, interrupted_ago=timedelta(minutes=i)) for i in (1, 5, 10)]
    v = verdict(three)
    assert (v.count_in_window, v.should_trip, v.tripped) == (3, True, False)
    two = verdict(three[:2])
    assert (two.count_in_window, two.should_trip) == (2, False)


def test_window_edges_are_inclusive_and_future_interrupts_are_ignored() -> None:
    on_edge = interrupt(1, interrupted_ago=timedelta(seconds=WINDOW))
    just_outside = interrupt(2, interrupted_ago=timedelta(seconds=WINDOW + 1))
    at_now = interrupt(3, interrupted_ago=timedelta(0))
    future = interrupt(4, interrupted_ago=timedelta(seconds=-1), started_ago=timedelta(hours=1))
    v = verdict([on_edge, just_outside, at_now, future], k=9)
    assert v.counted_attempt_ids == ("att_1", "att_3")


def test_an_interrupt_is_counted_by_the_time_it_was_interrupted() -> None:
    """R-1: started two hours ago, interrupted now: inside a 30 minute window."""
    long_runner = interrupt(1, started_ago=timedelta(hours=2), interrupted_ago=timedelta(0))
    assert verdict([long_runner], k=1).count_in_window == 1
    died_long_ago = interrupt(
        2, started_ago=timedelta(hours=2, minutes=5), interrupted_ago=timedelta(hours=2)
    )
    assert verdict([died_long_ago], k=1).count_in_window == 0


@pytest.mark.parametrize("cause", [HOST, STUDIO])
def test_studio_and_host_interruptions_count(cause: AttemptEndCause) -> None:
    assert verdict([interrupt(1, cause)], k=1).should_trip


def test_what_does_not_count() -> None:
    rows = [
        interrupt(1, USER),  # R-2: a user stop is not a crash
        interrupt(2, ENGINE),  # engine-side: consumes the retry budget instead
        interrupt(3, STUDIO, started_ago=None),  # R-3: never started
        interrupt(4, HOST, started_ago=None),
        Attempt(
            attempt_id=AttemptId("att_5"),
            job_id=JobId("job_5"),
            number=1,
            initiated_by=AttemptInitiator.AUTOMATIC,
            state=AttemptState.FAILED,
            cause=ENGINE,
            error_class=ErrorClass.INFRASTRUCTURE,
            started_at=NOW - timedelta(minutes=3),
            finished_at=NOW - timedelta(minutes=2),
        ),
    ]
    assert not any(counts_toward_breaker(a) for a in rows)
    v = verdict(rows, k=1)
    assert (v.count_in_window, v.should_trip) == (0, False)


def test_a_duplicate_row_counts_once() -> None:
    row = interrupt(1)
    assert verdict([row, row], k=2).count_in_window == 1


def test_counted_ids_are_oldest_first() -> None:
    rows = [
        interrupt(1, interrupted_ago=timedelta(minutes=1)),
        interrupt(2, interrupted_ago=timedelta(minutes=9)),
        interrupt(3, interrupted_ago=timedelta(minutes=5)),
    ]
    assert verdict(rows, k=9).counted_attempt_ids == ("att_2", "att_3", "att_1")


# ---------------------------------------------------------------- K and T are parameters; unset fails closed


def test_unset_k_fails_closed_as_k_one() -> None:
    assert FAIL_CLOSED_K == 1
    assert verdict([], k=None).should_trip is False
    assert verdict([interrupt(1)], k=None).should_trip is True
    assert verdict([interrupt(1)], k=3).should_trip is False


def test_unset_window_is_unbounded() -> None:
    old = interrupt(1, started_ago=timedelta(days=40), interrupted_ago=timedelta(days=30))
    assert verdict([old], k=1, window_s=None).count_in_window == 1
    assert verdict([old], k=1, window_s=WINDOW).count_in_window == 0


def test_parameters_are_validated() -> None:
    for bad in (0, -1, True, 1.5):
        with pytest.raises(DomainValidationError) as err:
            verdict([], k=bad)
        assert _code(err) == "breaker.k"
        with pytest.raises(DomainValidationError) as err2:
            verdict([], window_s=bad)
        assert _code(err2) == "breaker.window_s"
    with pytest.raises(DomainValidationError) as err3:
        verdict([], now=datetime(2026, 3, 1, 12, 0))
    assert _code(err3) == "time.naive"


# ---------------------------------------------------------------- trip, reset, quarantine


def test_a_tripped_breaker_stays_tripped_until_a_human_resets_it() -> None:
    tripped = event(1, BreakerEventKind.TRIPPED, NOW - timedelta(days=30))
    v = verdict([interrupt(1), interrupt(2), interrupt(3)], [tripped])
    assert v.tripped is True
    assert v.should_trip is False  # already tripped
    assert v.count_in_window == 3
    assert v.quarantined == {"cap.t2i"}
    long_after = verdict([], [tripped], now=NOW + timedelta(days=365))
    assert long_after.tripped and long_after.quarantined == {"cap.t2i"}  # nothing resets with time


def test_reset_clears_the_trip_and_the_count_but_not_the_quarantine() -> None:
    tripped = event(1, BreakerEventKind.TRIPPED, NOW - timedelta(minutes=20))
    reset = event(2, BreakerEventKind.RESET, NOW - timedelta(minutes=10))
    before = interrupt(1, interrupted_ago=timedelta(minutes=15))
    after = [interrupt(i, interrupted_ago=timedelta(minutes=2)) for i in (2, 3, 4)]
    v = verdict([before, *after[:1]], [tripped, reset], k=2)
    assert (v.tripped, v.count_in_window, v.should_trip) == (False, 1, False)
    assert v.quarantined == {"cap.t2i"}  # lifting a quarantine is a separate human action
    again = verdict([before, *after], [tripped, reset], k=2)
    assert (again.count_in_window, again.should_trip) == (3, True)


def test_an_interrupt_exactly_at_the_reset_is_cleared() -> None:
    reset_at = NOW - timedelta(minutes=10)
    at_reset = interrupt(1, interrupted_ago=timedelta(minutes=10))
    assert (
        verdict([at_reset], [event(1, BreakerEventKind.RESET, reset_at)], k=1).count_in_window == 0
    )


def test_quarantine_is_lifted_only_by_its_own_event() -> None:
    trip_a = event(1, BreakerEventKind.TRIPPED, NOW - timedelta(minutes=30), binding_id="cap.a")
    trip_b = event(2, BreakerEventKind.TRIPPED, NOW - timedelta(minutes=29), binding_id="cap.b")
    lift_a = event(
        3, BreakerEventKind.QUARANTINE_LIFTED, NOW - timedelta(minutes=5), binding_id="cap.a"
    )
    assert verdict([], [trip_a, trip_b]).quarantined == {"cap.a", "cap.b"}
    after = verdict([], [trip_a, trip_b, lift_a])
    assert after.quarantined == {"cap.b"}
    assert after.tripped is True  # lifting a quarantine does not reset the breaker
    retrip = event(4, BreakerEventKind.TRIPPED, NOW - timedelta(minutes=1), binding_id="cap.a")
    assert verdict([], [trip_a, trip_b, lift_a, retrip]).quarantined == {"cap.a", "cap.b"}


def test_event_order_in_the_input_does_not_matter() -> None:
    events = [
        event(1, BreakerEventKind.TRIPPED, NOW - timedelta(minutes=30)),
        event(2, BreakerEventKind.RESET, NOW - timedelta(minutes=20)),
        event(3, BreakerEventKind.TRIPPED, NOW - timedelta(minutes=10), binding_id="cap.b"),
        event(4, BreakerEventKind.QUARANTINE_LIFTED, NOW - timedelta(minutes=5)),
    ]
    expected = verdict([], events)
    rnd = random.Random(7)
    for _ in range(20):
        shuffled = events[:]
        rnd.shuffle(shuffled)
        assert verdict([], shuffled) == expected
    assert expected.tripped and expected.quarantined == {"cap.b"}


# ---------------------------------------------------------------- event validation


def test_event_rules() -> None:
    at = NOW
    trip = event(1, BreakerEventKind.TRIPPED, at)
    assert trip.k == 3 and trip.window_s == WINDOW
    unset = event(1, BreakerEventKind.TRIPPED, at, k=None, window_s=None)
    assert unset.k is None and unset.window_s is None  # fail-closed trips record "not configured"
    bad: list[tuple[dict[str, Any], BreakerEventKind, str]] = [
        ({"by_name": "ada"}, BreakerEventKind.TRIPPED, "breaker.actor"),
        ({"attempt_ids": ()}, BreakerEventKind.TRIPPED, "breaker.trip"),
        ({"binding_id": None}, BreakerEventKind.TRIPPED, "breaker.trip"),
        ({"by_name": None}, BreakerEventKind.RESET, "breaker.actor"),
        ({"attempt_ids": ("att_1",)}, BreakerEventKind.RESET, "breaker.fields"),
        ({"binding_id": "cap.t2i"}, BreakerEventKind.RESET, "breaker.fields"),
        ({"k": 3}, BreakerEventKind.QUARANTINE_LIFTED, "breaker.fields"),
        ({"binding_id": None}, BreakerEventKind.QUARANTINE_LIFTED, "breaker.fields"),
        ({"k": 0}, BreakerEventKind.TRIPPED, "breaker.k"),
        ({"at": datetime(2026, 1, 1)}, BreakerEventKind.RESET, "time.naive"),
    ]
    for override, kind, code in bad:
        args = dict(override)
        when = args.pop("at", at)
        with pytest.raises(DomainValidationError) as err:
            event(1, kind, when, **args)
        assert _code(err) == code, (kind, override)


# ---------------------------------------------------------------- startup marking

BOOT_AT = NOW - timedelta(hours=1)


def make_job(job_id: str, state: JobState) -> Job:
    kwargs: dict[str, Any] = {
        "job_id": job_id,
        "project_id": "proj_1",
        "task": TaskSpec(
            creative=CreativeSpec(operation=Operation.T2I, binding_id="cap.t2i", seed=1)
        ),
        "state": state,
        "identity_hash": IDENT,
        "task_fingerprint": TASKFP,
        "recovery_policy": RecoveryPolicySnapshot.no_automatic_recovery(),
    }
    return Job(**kwargs)


def live(
    n: int, state: AttemptState, *, started_ago: timedelta | None, job_id: str | None = None
) -> Attempt:
    kwargs: dict[str, Any] = {
        "attempt_id": f"att_{n}",
        "job_id": job_id or f"job_{n}",
        "number": 1,
        "initiated_by": AttemptInitiator.AUTOMATIC,
        "state": state,
    }
    if started_ago is not None:
        kwargs["started_at"] = NOW - started_ago
    return Attempt(**kwargs)


def mark(jobs: list[Job], attempts: list[Attempt]) -> Any:
    return startup_interruptions(jobs, attempts, boot_id="boot_1", host_boot_at=BOOT_AT, now=NOW)


def interruptions(changes: Any) -> dict[str, AttemptInterruption]:
    return {c.attempt_id: c for c in changes if isinstance(c, AttemptInterruption)}


def test_live_attempts_become_interrupted_with_the_right_cause() -> None:
    rows = [
        live(1, AttemptState.RUNNING, started_ago=timedelta(hours=3)),  # started before the boot
        live(2, AttemptState.RUNNING, started_ago=timedelta(minutes=10)),  # after the boot
        live(3, AttemptState.INGESTING, started_ago=timedelta(hours=2)),
        live(4, AttemptState.PENDING, started_ago=None),  # never started
        live(5, AttemptState.RUNNING, started_ago=timedelta(hours=1)),  # exactly at the boot time
    ]
    changes = interruptions(mark([], rows))
    assert {a: c.cause for a, c in changes.items()} == {
        "att_1": HOST,
        "att_2": STUDIO,
        "att_3": HOST,
        "att_4": STUDIO,
        "att_5": STUDIO,
    }
    assert all(c.finished_at == NOW and c.interrupted_boot_id == "boot_1" for c in changes.values())
    assert changes["att_3"].source is AttemptState.INGESTING


def test_terminal_attempts_are_never_selected() -> None:
    done = [
        interrupt(1),
        Attempt(
            attempt_id=AttemptId("att_2"),
            job_id=JobId("job_2"),
            number=1,
            initiated_by=AttemptInitiator.AUTOMATIC,
            state=AttemptState.CANCELED,
            started_at=NOW - timedelta(hours=2),
        ),
    ]
    assert mark([], done) == ()


def test_jobs_follow_their_attempts() -> None:
    jobs = [
        make_job("job_1", JobState.RUNNING),
        make_job("job_2", JobState.INGESTING),
        make_job("job_3", JobState.WAITING_RESOURCES),
        make_job(
            "job_4", JobState.WAITING_RESOURCES
        ),  # no pending attempt: stays queued for admission
        make_job("job_5", JobState.READY),
        make_job("job_6", JobState.RETRY_PENDING),
    ]
    rows = [
        live(1, AttemptState.RUNNING, started_ago=timedelta(minutes=5)),
        live(2, AttemptState.INGESTING, started_ago=timedelta(minutes=5)),
        live(3, AttemptState.PENDING, started_ago=None),
    ]
    changes = mark(jobs, rows)
    job_changes = [c for c in changes if isinstance(c, JobTransition)]
    assert [(c.job_id, c.source, c.target) for c in job_changes] == [
        ("job_1", JobState.RUNNING, JobState.INTERRUPTED),
        ("job_2", JobState.INGESTING, JobState.INTERRUPTED),
        ("job_3", JobState.WAITING_RESOURCES, JobState.READY),
    ]


def test_marking_is_idempotent() -> None:
    jobs = [make_job("job_1", JobState.RUNNING), make_job("job_2", JobState.WAITING_RESOURCES)]
    rows = [
        live(1, AttemptState.RUNNING, started_ago=timedelta(hours=3)),
        live(2, AttemptState.PENDING, started_ago=None),
    ]
    first = mark(jobs, rows)
    assert len(first) == 4
    new_rows = list(rows)
    new_jobs = list(jobs)
    for change in first:
        if isinstance(change, AttemptInterruption):
            idx = next(i for i, a in enumerate(new_rows) if a.attempt_id == change.attempt_id)
            new_rows[idx] = apply_attempt_interruption(new_rows[idx], change)
        else:
            idx = next(i for i, j in enumerate(new_jobs) if j.job_id == change.job_id)
            new_jobs[idx] = apply_job_transition(new_jobs[idx], change)
    assert {a.state for a in new_rows} == {AttemptState.INTERRUPTED}
    assert [j.state for j in new_jobs] == [JobState.INTERRUPTED, JobState.READY]
    assert mark(new_jobs, new_rows) == ()  # a second run in the same boot finds nothing
    assert (
        startup_interruptions(
            new_jobs, new_rows, boot_id="boot_2", host_boot_at=BOOT_AT, now=NOW + timedelta(hours=1)
        )
        == ()
    )


def test_output_is_deterministic_whatever_the_input_order() -> None:
    jobs = [make_job(f"job_{n}", JobState.RUNNING) for n in (3, 1, 2)]
    rows = [live(n, AttemptState.RUNNING, started_ago=timedelta(minutes=5)) for n in (3, 1, 2)]
    expected = mark(jobs, rows)
    assert mark(list(reversed(jobs)), list(reversed(rows))) == expected
    assert [c.attempt_id for c in expected if isinstance(c, AttemptInterruption)] == [
        "att_1",
        "att_2",
        "att_3",
    ]


def test_marked_started_attempts_feed_the_breaker_by_interruption_time() -> None:
    """R-1 end to end: started two hours ago, marked now, inside a 30 minute window; the unstarted one is not."""
    rows = [
        live(1, AttemptState.RUNNING, started_ago=timedelta(hours=2)),
        live(2, AttemptState.PENDING, started_ago=None),
    ]
    changes = interruptions(mark([], rows))
    marked = [apply_attempt_interruption(a, changes[a.attempt_id]) for a in rows]
    assert [counts_toward_breaker(a) for a in marked] == [True, False]
    v = breaker_verdict(marked, [], k=1, window_s=WINDOW, now=NOW)
    assert v.count_in_window == 1 and v.should_trip
    assert v.counted_attempt_ids == ("att_1",)


def test_an_interrupted_running_attempt_consumes_no_budget_but_counts_toward_the_breaker() -> None:
    """B-11 D2: a running attempt always has its start time, so marking it is a counted crash, not a free
    re-dispatch."""
    row = live(1, AttemptState.RUNNING, started_ago=timedelta(minutes=10))
    marked = apply_attempt_interruption(row, interruptions(mark([], [row]))["att_1"])
    assert marked.started_at is not None
    assert not consumes_budget(marked)
    assert counts_toward_breaker(marked)
    follower = Attempt(
        attempt_id=AttemptId("att_2"),
        job_id=JobId("job_1"),
        number=2,
        initiated_by=AttemptInitiator.AUTOMATIC,
        origin=AttemptOrigin.INFRASTRUCTURE_RETRY,
        previous_attempt_id=AttemptId("att_1"),
        state=AttemptState.PENDING,
    )
    assert retries_used([marked, follower]) == 0
    assert breaker_verdict([marked], [], k=1, window_s=WINDOW, now=NOW).should_trip


def test_a_running_attempt_cannot_lack_its_start_time() -> None:
    with pytest.raises(DomainValidationError) as err:
        live(1, AttemptState.RUNNING, started_ago=None)
    assert _code(err) == "attempt.started_required"


def test_the_host_cannot_boot_after_the_marking() -> None:
    """B-11 C3: host_boot_at later than now would blame the wrong cause."""
    later = NOW + timedelta(seconds=1)
    with pytest.raises(DomainValidationError) as err:
        startup_interruptions([], [], boot_id="boot_1", host_boot_at=later, now=NOW)
    assert _code(err) == "startup.clock"
    assert startup_interruptions([], [], boot_id="boot_1", host_boot_at=NOW, now=NOW) == ()


def test_clock_skew_and_naive_times_are_rejected() -> None:
    future = [live(1, AttemptState.RUNNING, started_ago=timedelta(minutes=-5))]
    with pytest.raises(DomainValidationError) as err:
        mark([], future)
    assert _code(err) == "startup.clock"
    for kwargs in (
        {"host_boot_at": datetime(2026, 3, 1, 11, 0), "now": NOW},
        {"host_boot_at": BOOT_AT, "now": datetime(2026, 3, 1, 12, 0)},
    ):
        with pytest.raises(DomainValidationError) as err2:
            startup_interruptions([], [], boot_id="boot_1", **kwargs)
        assert _code(err2) == "time.naive"
    with pytest.raises(DomainValidationError):
        startup_interruptions([], [], boot_id=" ", host_boot_at=BOOT_AT, now=NOW)


def test_apply_helpers_check_what_they_change() -> None:
    row = live(1, AttemptState.RUNNING, started_ago=timedelta(minutes=5))
    change = interruptions(mark([], [row]))["att_1"]
    done = apply_attempt_interruption(row, change)
    assert (done.state, done.cause, done.finished_at, done.interrupted_boot_id) == (
        AttemptState.INTERRUPTED,
        STUDIO,
        NOW,
        "boot_1",
    )
    with pytest.raises(DomainValidationError) as err:
        apply_attempt_interruption(
            done, change
        )  # already interrupted: not the state the change expects
    assert _code(err) == "startup.apply"
    job = make_job("job_1", JobState.RUNNING)
    moved = apply_job_transition(
        job, JobTransition(job_id="job_1", source=JobState.RUNNING, target=JobState.INTERRUPTED)
    )
    assert moved.state is JobState.INTERRUPTED
    with pytest.raises(InvalidTransition):
        apply_job_transition(
            job, JobTransition(job_id="job_1", source=JobState.RUNNING, target=JobState.QUEUED)
        )
    with pytest.raises(DomainValidationError):
        apply_job_transition(
            job, JobTransition(job_id="job_9", source=JobState.RUNNING, target=JobState.INTERRUPTED)
        )
