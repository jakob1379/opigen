from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from backup.cli import main
from backup.docker_client import ImageMetadata, WorkerResult
from backup.restic import ResticRunner
from backup.restore import RestoreError, RestoreManager
from backup.state import BackupRecord, BackupState, StateStore
from tests.fakes import FakeDockerClient


def test_restore_plan_detects_image_drift(app_config):
    docker_client = FakeDockerClient()
    docker_client.images["fixture:latest"] = ImageMetadata(
        reference="fixture:latest",
        image_id="sha256:changed",
        repo_digests=("fixture@sha256:changed",),
    )
    state_store = StateStore(app_config.state.path)
    state_store.save(BackupState(backups=[_record()]))
    manager = RestoreManager(
        config=app_config,
        docker_client=docker_client,
        restic_runner=ResticRunner(app_config, docker_client),
        state_store=state_store,
    )

    plan = manager.plan("backup-1")

    assert plan.image_check.drifted is True
    assert plan.image_check.expected_image_id == "sha256:fixture"
    assert plan.image_check.current_image_id == "sha256:changed"


def test_restore_volume_refuses_drift_without_override(app_config):
    docker_client = FakeDockerClient()
    docker_client.images["fixture:latest"] = ImageMetadata(
        reference="fixture:latest",
        image_id="sha256:changed",
        repo_digests=("fixture@sha256:changed",),
    )
    state_store = StateStore(app_config.state.path)
    state_store.save(BackupState(backups=[_record()]))
    manager = RestoreManager(
        config=app_config,
        docker_client=docker_client,
        restic_runner=ResticRunner(app_config, docker_client),
        state_store=state_store,
    )

    try:
        manager.restore_volume("backup-1")
    except RestoreError as exc:
        assert "Image identity has drifted" in str(exc)
    else:
        raise AssertionError("restore should refuse image drift")

    assert docker_client.created_volumes == []


def test_restore_volume_mounts_new_volume_at_original_destination(app_config):
    docker_client = FakeDockerClient()
    docker_client.results = [
        WorkerResult(0, "snapshots"),
        WorkerResult(0, "restore ok"),
    ]
    state_store = StateStore(app_config.state.path)
    state_store.save(BackupState(backups=[_record()]))
    manager = RestoreManager(
        config=app_config,
        docker_client=docker_client,
        restic_runner=ResticRunner(app_config, docker_client),
        state_store=state_store,
    )

    plan = manager.restore_volume("backup-1", target_volume="restored-data")

    assert plan.target_volume == "restored-data"
    assert docker_client.created_volumes == ["restored-data"]
    restore_call = docker_client.worker_calls[1]
    assert restore_call["command"] == [
        "restic",
        "restore",
        "--verbose",
        "snapshot123",
        "--target",
        "/",
        "--include",
        "/data",
    ]
    assert restore_call["worker_mounts"][-1].source == "restored-data"
    assert restore_call["worker_mounts"][-1].target == "/data"


def test_restore_cli_lists_and_plans_backups(tmp_path: Path, secret_files, monkeypatch, capsys):
    state_path = tmp_path / "state.json"
    StateStore(state_path).save(BackupState(backups=[_record()]))
    config_path = tmp_path / "backup.toml"
    config_path.write_text(
        f"""
[backup]
repository = "s3:http://example/backups"
password_file = "{secret_files["password"]}"

[state]
path = "{state_path}"

[runtime]
worker_image = "worker:test"
""".strip(),
        encoding="utf-8",
    )
    docker_client = FakeDockerClient()
    monkeypatch.setattr("backup.cli.DockerClient", lambda: docker_client)

    assert main(["restore", "list", "--config", str(config_path)]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed[0]["id"] == "backup-1"

    assert main(["restore", "plan", "backup-1", "--config", str(config_path)]) == 0
    planned = json.loads(capsys.readouterr().out)
    assert planned["target_volume"] == "opigen-restore-db-data-20260614100000"
    assert planned["image_check"]["drifted"] is False


def _record() -> BackupRecord:
    completed_at = datetime(2026, 6, 14, 10, 0, tzinfo=UTC)
    return BackupRecord(
        id="backup-1",
        group="db",
        container_name="database",
        container_id="container-id",
        volume_name="db-data",
        volume_destination="/data",
        image_reference="fixture:latest",
        image_id="sha256:fixture",
        repo_digest="fixture@sha256:digest",
        started_at=completed_at,
        completed_at=completed_at,
        outcome="success",
        snapshot_id="snapshot123",
        snapshot_paths=("/data",),
    )
