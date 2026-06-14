from __future__ import annotations

from datetime import UTC, datetime, time

from backup.config import ScheduleConfig
from backup.docker_client import WorkerResult
from backup.restic import ResticRunner, repository_needs_init
from backup.scheduler import calculate_next_run
from tests.fakes import FakeContainer, FakeDockerClient


def test_restic_command_and_environment(app_config):
    docker_client = FakeDockerClient()
    docker_client.results.extend(
        [
            WorkerResult(exit_code=0, output="snapshots"),
            WorkerResult(exit_code=0, output="backup"),
        ]
    )
    runner = ResticRunner(app_config, docker_client)
    container = FakeContainer("db")

    result = runner.backup_volume(container, "/data", ("--exclude", "*.tmp"))

    assert result.command == ("restic", "backup", "--verbose", "--exclude", "*.tmp", "/data")
    backup_call = docker_client.worker_calls[1]
    assert backup_call["image"] == "worker:test"
    assert backup_call["source_container"] is container
    assert backup_call["environment"]["RESTIC_REPOSITORY"] == "s3:http://example/backups"
    assert backup_call["environment"]["RESTIC_PASSWORD"] == "restic-pass"
    assert backup_call["environment"]["AWS_ACCESS_KEY_ID"] == "access-key"


def test_restic_initializes_missing_repository(app_config):
    docker_client = FakeDockerClient()
    docker_client.results.extend(
        [
            WorkerResult(exit_code=1, output="unable to open config file"),
            WorkerResult(exit_code=0, output="created restic repository"),
            WorkerResult(exit_code=0, output="backup"),
        ]
    )
    runner = ResticRunner(app_config, docker_client)

    runner.backup_volume(FakeContainer("db"), "/data", ())

    assert docker_client.worker_calls[0]["command"] == ["restic", "snapshots", "--verbose"]
    assert docker_client.worker_calls[1]["command"] == ["restic", "init", "--verbose"]


def test_repository_needs_init_detection():
    assert repository_needs_init("Fatal: unable to open config file")
    assert not repository_needs_init("Fatal: wrong password or no key found")


def test_calculate_next_hourly_run():
    now = datetime(2026, 6, 13, 10, 15, tzinfo=UTC)

    assert calculate_next_run(now, ScheduleConfig(frequency="hourly")) == datetime(
        2026, 6, 13, 11, 0, tzinfo=UTC
    )


def test_calculate_next_daily_run():
    now = datetime(2026, 6, 13, 1, 0, tzinfo=UTC)

    assert calculate_next_run(
        now,
        ScheduleConfig(frequency="daily", preferred_time=time(2, 0)),
    ) == datetime(2026, 6, 13, 2, 0, tzinfo=UTC)


def test_calculate_next_weekly_run_uses_monday():
    now = datetime(2026, 6, 13, 10, 0, tzinfo=UTC)

    assert calculate_next_run(
        now,
        ScheduleConfig(frequency="weekly", preferred_time=time(2, 0)),
    ) == datetime(2026, 6, 15, 2, 0, tzinfo=UTC)
