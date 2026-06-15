from __future__ import annotations

import pytest

from backup.config import ConfigError, parse_config
from backup.docker_client import Mount
from backup.labels import LabelError, parse_container_labels, parse_mount_selection
from backup.volumes import MountSelectionError, select_backup_mounts


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
    assert config.logging.level == "info"
    assert config.logging.format == "json"
    assert config.discovery.default_mounts == "named"


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


def test_parse_config_logging_and_discovery(secret_files):
    config = parse_config(
        {
            "backup": {
                "repository": "/restic-repo",
                "password_file": str(secret_files["password"]),
            },
            "logging": {
                "level": "debug",
                "format": "console",
            },
            "discovery": {
                "default_mounts": "all",
                "unreadable_mount": "skip",
            },
        }
    )

    assert config.logging.level == "debug"
    assert config.logging.format == "console"
    assert config.discovery.default_mounts == "all"


def test_parse_config_requires_repository(secret_files):
    with pytest.raises(ConfigError, match="repository"):
        parse_config({"backup": {"password_file": str(secret_files["password"])}})


def test_parse_container_labels():
    labels = parse_container_labels(
        {
            "backup.enabled": "true",
            "backup.group": "db",
            "backup.stop": "false",
            "backup.restic_args": "--exclude '*.tmp'",
            "backup.mounts": "pgdata,/config",
            "backup.pre_stop_signal": "USR1",
            "backup.pre_stop_wait_seconds": "5",
        }
    )

    assert labels.group == "db"
    assert labels.stop is False
    assert labels.restic_args == ("--exclude", "*.tmp")
    assert labels.mounts is not None
    assert labels.mounts.selectors == ("pgdata", "/config")
    assert labels.pre_stop_signal == "USR1"
    assert labels.pre_stop_wait_seconds == 5


def test_parse_container_labels_requires_group():
    with pytest.raises(LabelError, match="backup.group"):
        parse_container_labels({"backup.enabled": "true"})


def test_parse_mount_selection_defaults_to_all():
    assert parse_mount_selection("").all_mounts is True
    assert parse_mount_selection("all").all_mounts is True


def test_default_mount_selection_uses_named_volumes_only(secret_files):
    config = parse_config(
        {
            "backup": {
                "repository": "/restic-repo",
                "password_file": str(secret_files["password"]),
            }
        }
    )
    mounts = [
        Mount(type="volume", name="data", destination="/data"),
        Mount(type="bind", name=None, destination="/host", source="/srv/host"),
    ]

    selected = select_backup_mounts(mounts, None, config.discovery)

    assert [item.mount.destination for item in selected] == ["/data"]


def test_select_all_mounts_includes_bind_mounts(secret_files):
    config = parse_config(
        {
            "backup": {
                "repository": "/restic-repo",
                "password_file": str(secret_files["password"]),
            }
        }
    )
    mounts = [
        Mount(type="volume", name="data", destination="/data"),
        Mount(type="bind", name=None, destination="/host", source="/srv/host"),
        Mount(type="tmpfs", name=None, destination="/tmpfs"),
    ]

    selected = select_backup_mounts(mounts, parse_mount_selection("all"), config.discovery)

    assert [item.mount.destination for item in selected if item.selected] == ["/data", "/host"]
    assert selected[2].skip_reason == "unsupported mount type: tmpfs"
    assert selected[1].worker_mounts[0].source == "/srv/host"
    assert selected[1].worker_mounts[0].mode == "ro"


def test_select_mounts_by_name_and_destination(secret_files):
    config = parse_config(
        {
            "backup": {
                "repository": "/restic-repo",
                "password_file": str(secret_files["password"]),
            }
        }
    )
    mounts = [
        Mount(type="volume", name="data", destination="/data"),
        Mount(type="bind", name=None, destination="/config", source="/srv/config"),
    ]

    selected = select_backup_mounts(mounts, parse_mount_selection("data,/config"), config.discovery)

    assert [item.mount.destination for item in selected] == ["/data", "/config"]


def test_unmatched_selector_is_container_error(secret_files):
    config = parse_config(
        {
            "backup": {
                "repository": "/restic-repo",
                "password_file": str(secret_files["password"]),
            }
        }
    )
    mounts = [Mount(type="volume", name="data", destination="/data")]

    with pytest.raises(MountSelectionError, match="missing"):
        select_backup_mounts(mounts, parse_mount_selection("missing"), config.discovery)
