"""Transition tables: integrity, terminal states, and random walks that only use table edges."""

from __future__ import annotations

from enum import StrEnum

import pytest
from hypothesis import given
from hypothesis import strategies as st

from studio.domain.errors import InvalidTransition
from studio.domain.states import (
    ALL_TABLES,
    APPROVAL_TRANSITIONS,
    JOB_TRANSITIONS,
    ApprovalState,
    AttemptState,
    JobState,
    TransitionTable,
    check_all_tables,
    check_table_integrity,
)

TABLE_NAMES = sorted(ALL_TABLES)


def test_every_table_is_consistent() -> None:
    assert check_all_tables() == ()


@pytest.mark.parametrize("name", TABLE_NAMES)
def test_each_state_has_a_row_and_targets_are_states(name: str) -> None:
    table, enum_cls = ALL_TABLES[name]
    assert set(table.edges) == set(enum_cls)
    for targets in table.edges.values():
        assert targets <= set(enum_cls)


@pytest.mark.parametrize("name", TABLE_NAMES)
def test_terminal_states_have_no_successors(name: str) -> None:
    table, _ = ALL_TABLES[name]
    assert table.terminal_states
    for state in table.terminal_states:
        assert table.successors(state) == frozenset()
        assert table.is_terminal(state)
        with pytest.raises(InvalidTransition):
            table.require_transition(state, table.initial)


@pytest.mark.parametrize("name", TABLE_NAMES)
def test_initial_state_is_not_terminal(name: str) -> None:
    table, _ = ALL_TABLES[name]
    assert not table.is_terminal(table.initial)


def _walk(table: TransitionTable, data: st.DataObject, limit: int) -> list[StrEnum]:
    path: list[StrEnum] = [table.initial]
    for _ in range(limit):
        successors = sorted(table.successors(path[-1]), key=str)
        if not successors:
            break
        path.append(data.draw(st.sampled_from(successors)))
    return path


@pytest.mark.parametrize("name", TABLE_NAMES)
@given(data=st.data())
def test_random_walks_use_only_table_edges(name: str, data: st.DataObject) -> None:
    table, _ = ALL_TABLES[name]
    path = _walk(table, data, 40)
    for source, target in zip(path, path[1:], strict=False):
        assert table.can_transition(source, target)
        table.require_transition(source, target)
    last = path[-1]
    assert table.is_terminal(last) or len(path) == 41


def test_integrity_check_reports_a_broken_table() -> None:
    class Light(StrEnum):
        RED = "red"
        GREEN = "green"
        ORPHAN = "orphan"

    broken = TransitionTable[Light](
        name="light",
        initial=Light.RED,
        edges={Light.RED: frozenset({Light.RED, Light.GREEN}), Light.GREEN: frozenset()},
    )
    codes = {i.code for i in check_table_integrity(broken, Light)}
    assert {"table.missing_row", "table.self_loop", "table.unreachable"} <= codes


def test_approval_rejection_and_staleness_edges_are_unchanged_by_this_part() -> None:
    # Approval changes (REJECTED terminal, stale_at, ...) belong to part 2; this part must not touch them.
    assert APPROVAL_TRANSITIONS.successors(ApprovalState.REJECTED) == {ApprovalState.STALE}


def test_job_and_attempt_edges_used_by_startup_marking_exist() -> None:
    for source in (JobState.RUNNING, JobState.INGESTING):
        assert JOB_TRANSITIONS.can_transition(source, JobState.INTERRUPTED)
    assert JOB_TRANSITIONS.can_transition(JobState.WAITING_RESOURCES, JobState.READY)
    assert JOB_TRANSITIONS.can_transition(JobState.WAITING_RESOURCES, JobState.FAILED)
    for source in (AttemptState.PENDING, AttemptState.RUNNING, AttemptState.INGESTING):
        assert ALL_TABLES["attempt"][0].can_transition(source, AttemptState.INTERRUPTED)
