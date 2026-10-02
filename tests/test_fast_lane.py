from app.queue.state_machine import JobStatus
from tests.test_worker_tasks import MemoryWorld, FixtureBoards, FixtureSubmit, make_tasks, item


def add_job(world, *, status=JobStatus.FOUND):
    world.jobs[1] = {"status": status, "score": 8, "url": "https://jobs.example.com/python-engineer",
                     "description": "Build Python APIs with PostgreSQL.", "application_lane": "fast_lane"}


def test_event_flow_scores_tailors_fills_and_waits_for_review(tmp_path, project_root):
    world = MemoryWorld([], [])
    add_job(world)
    submit = FixtureSubmit()
    tasks = make_tasks(world, FixtureBoards([]), tmp_path, project_root, submitter=submit)
    tasks.run_job(item("run_job", {"trigger": "event", "lane": "fast_lane"}, 1))
    assert world.jobs[1]["status"] == JobStatus.FILLED
    assert not submit.calls
    assert [log["step"] for log in world.logs if log["step"] != "pipeline"] == ["score", "tailor", "fill"]
    assert all(log["trigger"] == "event" for log in world.logs)


def test_fast_lane_approval_and_retry_keep_separate_cap(tmp_path, project_root):
    world = MemoryWorld([], [])
    add_job(world, status=JobStatus.FILLED)
    world.settings.update(batch_daily_cap=10, fast_lane_daily_cap=2)
    submit = FixtureSubmit(JobStatus.APPROVED, "cap reached")
    tasks = make_tasks(world, FixtureBoards([]), tmp_path, project_root, submitter=submit)
    tasks.review_decision(item("review_decision", {"decision": "approve", "trigger": "manual"}, 1))
    assert world.jobs[1]["status"] == JobStatus.APPROVED
    assert submit.calls == [(1, 2, "fast_lane")]
    world.logs.clear()
    tasks.run_pipeline(item("run_pipeline", {"steps": ["submit"]}))
    assert submit.calls == [(1, 2, "fast_lane"), (1, 2, "fast_lane")]
