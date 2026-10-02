import json

import pytest

from app.config import RuntimeSettings
from app.steps.auto_submit import AutoSubmitFacts, can_auto_submit
from app.queue.state_machine import JobStatus
from tests.test_fast_lane import add_job
from tests.test_worker_tasks import MemoryWorld, FixtureBoards, FixtureSubmit, make_tasks, item


@pytest.fixture
def facts(project_root):
    return json.loads((project_root / "tests/fixtures/auto_submit_facts.json").read_text())


def test_auto_submit_requires_toggle_and_every_guard(facts):
    settings = RuntimeSettings(auto_submit=True, trusted_source_ids=[1])
    assert not can_auto_submit(AutoSubmitFacts(**facts), RuntimeSettings())
    assert can_auto_submit(AutoSubmitFacts(**facts), settings)
    for changes in ({"score": 7}, {"source_id": 2}, {"added_skill_count": 1},
                    {"outcome": "needs_manual"}, {"answers": []}, {"score": None}):
        assert not can_auto_submit(AutoSubmitFacts(**{**facts, **changes}), settings)
    for changes in ({"flag": "legal"}, {"source": "unknown"}, {"source": "drafted"},
                    {"value": None}, {"source": "invented"}, {"required": "true"}):
        changed = {**facts, "answers": [{**facts["answers"][0], **changes}]}
        assert not can_auto_submit(AutoSubmitFacts(**changed), settings)
    assert not can_auto_submit(None, settings)


def test_auto_approval_uses_runner_then_separate_submit(tmp_path, project_root, facts):
    world = MemoryWorld([], [])
    add_job(world)
    world.settings.update(auto_submit=True, trusted_source_ids=[1], fast_lane_daily_cap=3)
    world.auto_submit_facts = lambda job_id: AutoSubmitFacts(**facts)
    submit = FixtureSubmit()
    tasks = make_tasks(world, FixtureBoards([]), tmp_path, project_root, submitter=submit)
    tasks.run_job(item("run_job", {"trigger": "event"}, 1))
    assert world.jobs[1]["status"] == JobStatus.SUBMITTED
    assert submit.calls == [(1, 3, "fast_lane")]
    assert any(log["step"] == "auto_approve" and log["status"] == "approved" for log in world.logs)


def test_flagged_job_stays_in_review(tmp_path, project_root, facts):
    world = MemoryWorld([], [])
    add_job(world)
    world.settings.update(auto_submit=True, trusted_source_ids=[1])
    facts["answers"][0]["flag"] = "consent"
    world.auto_submit_facts = lambda job_id: AutoSubmitFacts(**facts)
    submit = FixtureSubmit()
    tasks = make_tasks(world, FixtureBoards([]), tmp_path, project_root, submitter=submit)
    tasks.run_job(item("run_job", {"trigger": "event"}, 1))
    assert world.jobs[1]["status"] == JobStatus.FILLED and not submit.calls


def test_auto_submit_policy_respects_custom_score_threshold(facts):
    settings = RuntimeSettings(auto_submit=True, auto_submit_score_threshold=9, trusted_source_ids=[1])
    assert not can_auto_submit(AutoSubmitFacts(**facts), settings)
    assert can_auto_submit(AutoSubmitFacts(**{**facts, "score": 9}), settings)
