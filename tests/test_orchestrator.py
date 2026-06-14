from __future__ import annotations

from backup.docker_client import Mount, WorkerResult
from backup.orchestrator import BackupOrchestrator
from backup.restic import ResticRunner
from backup.state import StateStore
from tests.fakes import FakeContainer, FakeDockerClient


def _container(name: str, group: str, *, status: str = "running", stop: str = "true"):
    return FakeContainer(
        name=name,
        status=status,
        labels={
            "backup.enabled": "true",
            "backup.group": group,
            "backup.stop": stop,
        },
        mounts=[Mount(type="volume", name=f"{name}-data", destination="/data")],
    )


def _orchestrator(app_config, docker_client):
    return BackupOrchestrator(
        config=app_config,
        docker_client=docker_client,
        restic_runner=ResticRunner(app_config, docker_client),
        state_store=StateStore(app_config.state.path),
    )


def test_groups_process_sequentially(app_config):
    first = _container("a", "a")
    second = _container("b", "b")
    docker_client = FakeDockerClient([second, first])
    docker_client.results = [WorkerResult(0, "") for _ in range(5)]

    assert _orchestrator(app_config, docker_client).run_once() is True

    backup_calls = [
        call for call in docker_client.worker_calls if call["command"][:2] == ["restic", "backup"]
    ]
    assert [call["source_container"].name for call in backup_calls] == ["a", "b"]


def test_stop_failure_skips_group_and_restores_already_stopped(app_config):
    first = _container("first", "db")
    second = _container("second", "db")
    second.stop_error = RuntimeError("nope")
    next_group = _container("next", "next")
    docker_client = FakeDockerClient([first, second, next_group])
    docker_client.results = [WorkerResult(0, "") for _ in range(4)]

    assert _orchestrator(app_config, docker_client).run_once() is False

    assert first.events == ["stop:30", "start"]
    assert second.events == ["stop:30"]
    backup_calls = [
        call for call in docker_client.worker_calls if call["command"][:2] == ["restic", "backup"]
    ]
    assert [call["source_container"].name for call in backup_calls] == ["next"]


def test_backup_failure_still_restarts_and_continues(app_config):
    first = _container("first", "a")
    second = _container("second", "b")
    docker_client = FakeDockerClient([first, second])
    docker_client.results = [
        WorkerResult(0, "snapshots"),
        WorkerResult(1, "backup failed"),
        WorkerResult(0, "backup ok"),
        WorkerResult(0, "forget"),
        WorkerResult(0, "check"),
    ]

    assert _orchestrator(app_config, docker_client).run_once() is False

    assert first.events == ["stop:30", "start"]
    assert second.events == ["stop:30", "start"]
    backup_calls = [
        call for call in docker_client.worker_calls if call["command"][:2] == ["restic", "backup"]
    ]
    assert [call["source_container"].name for call in backup_calls] == ["first", "second"]


def test_initially_stopped_container_is_not_started(app_config):
    stopped = _container("stopped", "db", status="exited")
    docker_client = FakeDockerClient([stopped])
    docker_client.results = [WorkerResult(0, "") for _ in range(4)]

    assert _orchestrator(app_config, docker_client).run_once() is True

    assert stopped.events == []
