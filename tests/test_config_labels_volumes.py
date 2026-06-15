from __future__ import annotations

import pytest

from backup.config import ConfigError, parse_config
from backup.docker_client import Mount
from backup.labels import LabelError, parse_container_labels, parse_volume_selection
from backup.volumes import VolumeSelectionError, select_named_volumes


def test_parse_config_defaults(secret_files):
    config = parse_config(
        {
            "backup": {
                "repository": "s3:http://repo",
                "password_file": str(secret_files["password"]),
            }
        }
    )

    assert config.runtime.worker_image == "opigen-backup:latest"
    assert config.state.path.as_posix() == "/state/backup_state.json"
    assert config.health.bind_host == "127.0.0.1"
    assert config.health.port == 8080
    assert config.schedule.frequency == "daily"
    assert config.restic.global_args == ()


def test_parse_config_health(secret_files):
    config = parse_config(
        {
            "backup": {
                "repository": "s3:http://repo",
                "password_file": str(secret_files["password"]),
            },
            "health": {
                "bind_host": "0.0.0.0",
                "port": 9090,
                "readiness_max_age_seconds": 300,
            },
        }
    )

    assert config.health.bind_host == "0.0.0.0"
    assert config.health.port == 9090
    assert config.health.readiness_max_age_seconds == 300


def test_parse_config_worker_mounts(secret_files):
    config = parse_config(
        {
            "backup": {
                "repository": "/restic-repo",
                "password_file": str(secret_files["password"]),
            },
            "runtime": {
                "worker_mounts": ["/tmp/opigen-restic:/restic-repo:rw"],
            },
        }
    )

    assert config.runtime.worker_mounts[0].source == "/tmp/opigen-restic"
    assert config.runtime.worker_mounts[0].target == "/restic-repo"
    assert config.runtime.worker_mounts[0].mode == "rw"


def test_parse_config_requires_repository(secret_files):
    with pytest.raises(ConfigError, match="repository"):
        parse_config({"backup": {"password_file": str(secret_files["password"])}})


def test_parse_container_labels():
    labels = parse_container_labels(
        {
            "backup.enabled": "true",
            "backup.group": "db",
            "backup.stop": "false",
            "backup.args": "--exclude '*.tmp'",
            "backup.volumes": "pgdata,/config",
        }
    )

    assert labels.group == "db"
    assert labels.stop is False
    assert labels.restic_args == ("--exclude", "*.tmp")
    assert labels.volumes.selectors == ("pgdata", "/config")


def test_parse_container_labels_requires_group():
    with pytest.raises(LabelError, match="backup.group"):
        parse_container_labels({"backup.enabled": "true"})


def test_parse_volume_selection_defaults_to_all():
    assert parse_volume_selection("").all_volumes is True
    assert parse_volume_selection("all").all_volumes is True


def test_select_all_named_volumes_rejects_binds():
    mounts = [
        Mount(type="volume", name="data", destination="/data"),
        Mount(type="bind", name=None, destination="/host"),
    ]

    selected = select_named_volumes(mounts, parse_volume_selection("all"))

    assert [mount.destination for mount in selected] == ["/data"]


def test_select_named_volumes_by_name_and_destination():
    mounts = [
        Mount(type="volume", name="data", destination="/data"),
        Mount(type="volume", name="config", destination="/config"),
    ]

    selected = select_named_volumes(mounts, parse_volume_selection("data,/config"))

    assert [mount.name for mount in selected] == ["data", "config"]


def test_unmatched_selector_is_container_error():
    mounts = [Mount(type="volume", name="data", destination="/data")]

    with pytest.raises(VolumeSelectionError, match="missing"):
        select_named_volumes(mounts, parse_volume_selection("missing"))
