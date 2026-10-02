import pytest

from app.queue.tasks import PipelineBusy
from tests.test_worker_tasks import MemoryWorld, FixtureBoards, make_tasks, item, listings


def test_full_run_locks_before_scraping(tmp_path, project_root, listings):
    world = MemoryWorld([], [])
    world.lock_available = False
    boards = FixtureBoards(listings)
    tasks = make_tasks(world, boards, tmp_path, project_root)
    with pytest.raises(PipelineBusy):
        tasks.handlers()["run_pipeline"](item("run_pipeline", {"steps": ["scrape"]}))
    assert boards.calls == 0 and not world.logs


def test_handler_releases_lock_on_error_and_success(tmp_path, project_root):
    world = MemoryWorld([], [])
    released = []
    world.release_lock = lambda: released.append(True)
    tasks = make_tasks(world, FixtureBoards([]), tmp_path, project_root)
    tasks.handlers()["run_pipeline"](item("run_pipeline", {"steps": []}))
    assert len(released) == 1
    with pytest.raises(ValueError):
        tasks.handlers()["run_job"](item("run_job", {}))
    assert len(released) == 2


def test_subscription_poll_uses_same_lock(tmp_path, project_root):
    world = MemoryWorld([], [])
    world.lock_available = False
    tasks = make_tasks(world, FixtureBoards([]), tmp_path, project_root)
    with pytest.raises(PipelineBusy):
        tasks.handlers()["poll_subscription"](item("poll_subscription", {"subscription_id": 1}))
