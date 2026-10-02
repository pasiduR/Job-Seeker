import json
from collections.abc import Callable
from pathlib import Path

import pytest

from app.queue.state_machine import (
    InvalidStatusTransition,
    JobStatus,
    mark_approved,
    mark_failed,
    mark_filled,
    mark_needs_manual,
    mark_scored,
    mark_skipped,
    mark_submitted,
    mark_tailored,
)


Transition = Callable[[JobStatus | str], JobStatus]
TRANSITIONS: dict[str, Transition] = {
    function.__name__: function
    for function in (
        mark_scored,
        mark_tailored,
        mark_filled,
        mark_approved,
        mark_submitted,
        mark_skipped,
        mark_failed,
        mark_needs_manual,
    )
}


@pytest.fixture
def status_cases(project_root: Path) -> dict[str, list[list[str]]]:
    return json.loads(
        (project_root / "tests/fixtures/status_transitions.json").read_text(
            encoding="utf-8"
        )
    )


def test_valid_status_transitions(
    status_cases: dict[str, list[list[str]]],
) -> None:
    for function_name, current, expected in status_cases["valid"]:
        transition = TRANSITIONS[function_name]

        assert transition(current) == JobStatus(expected)


def test_status_transitions_are_idempotent(
    status_cases: dict[str, list[list[str]]],
) -> None:
    for function_name, _, expected in status_cases["valid"]:
        transition = TRANSITIONS[function_name]

        assert transition(expected) == JobStatus(expected)


def test_invalid_status_transitions_are_rejected(
    status_cases: dict[str, list[list[str]]],
) -> None:
    for function_name, current in status_cases["invalid"]:
        with pytest.raises(InvalidStatusTransition):
            TRANSITIONS[function_name](current)
