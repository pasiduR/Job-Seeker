import pytest

from app.queue.tasks import PipelineBusy
from tests.test_worker_tasks import MemoryWorld, FixtureBoards, make_tasks, item, listings
from tests.test_queue import FakeQueue, RecordingQueueConnection, _load_queue_item
from app.queue.worker import Worker
from app.queue.postgres import PostgresQueue


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


def test_contention_never_exhausts_failure_budget(project_root):
    queued = _load_queue_item(project_root)
    queue = FakeQueue(queued)
    def busy(item):
        raise PipelineBusy("another run")
    worker = Worker(queue, "fixture-worker", {queued.task: busy})
    for _ in range(5):
        assert worker.run_once()
    assert queue.deferred == [queued.id] * 5
    assert not queue.retried and not queue.succeeded
    connection = RecordingQueueConnection(())
    PostgresQueue(connection).defer(queued, "fixture-worker")
    assert "attempts = greatest(attempts - 1, 0)" in connection.queries[-1]
