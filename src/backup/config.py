from __future__ import annotations

import shlex
import tomllib
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """Raised when the backup configuration is invalid."""


@dataclass(frozen=True)
class BackupRepositoryConfig:
    repository: str
    password_file: Path
    aws_access_key_id_file: Path | None = None
    aws_secret_access_key_file: Path | None = None


@dataclass(frozen=True)
class WorkerMount:
    source: str
    target: str
    mode: str = "rw"


@dataclass(frozen=True)
class RuntimeConfig:
    worker_image: str = "opigen-backup:latest"
    worker_mounts: tuple[WorkerMount, ...] = ()


@dataclass(frozen=True)
class StateConfig:
    path: Path = Path("/state/backup_state.json")


@dataclass(frozen=True)
class HealthConfig:
    bind_host: str = "127.0.0.1"
    port: int = 8080
    readiness_max_age_seconds: int | None = None


@dataclass(frozen=True)
class ScheduleConfig:
    enabled: bool = True
    frequency: str = "daily"
    preferred_time: time = time(hour=2)


@dataclass(frozen=True)
class PruneConfig:
    enabled: bool = True
    keep_hourly: int | None = 48
    keep_daily: int | None = 14
    keep_weekly: int | None = 4
    keep_monthly: int | None = 12
    keep_yearly: int | None = 10


@dataclass(frozen=True)
class CheckConfig:
    enabled: bool = True
    frequency: str = "weekly"


@dataclass(frozen=True)
class ResticConfig:
    global_args: tuple[str, ...] = ()


@dataclass(frozen=True)
class TimeoutConfig:
    stop_grace_period: int = 30
    backup_operation: int = 3600


@dataclass(frozen=True)
class AppConfig:
    backup: BackupRepositoryConfig
    runtime: RuntimeConfig = RuntimeConfig()
    state: StateConfig = StateConfig()
    health: HealthConfig = HealthConfig()
    schedule: ScheduleConfig = ScheduleConfig()
    prune: PruneConfig = PruneConfig()
    check: CheckConfig = CheckConfig()
    restic: ResticConfig = ResticConfig()
    timeouts: TimeoutConfig = TimeoutConfig()


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path)
    with config_path.open("rb") as fh:
        data = tomllib.load(fh)
    return parse_config(data)


def parse_config(data: dict[str, Any]) -> AppConfig:
    backup_data = _table(data, "backup", required=True)
    repository = _required_str(backup_data, "repository", "backup")
    password_file = Path(_required_str(backup_data, "password_file", "backup"))

    schedule_data = _table(data, "schedule")
    prune_data = _table(data, "prune")
    check_data = _table(data, "check")
    restic_data = _table(data, "restic")
    timeouts_data = _table(data, "timeouts")
    runtime_data = _table(data, "runtime")
    state_data = _table(data, "state")
    health_data = _table(data, "health")

    schedule_frequency = str(schedule_data.get("frequency", "daily"))
    _validate_frequency(schedule_frequency, "schedule.frequency")

    check_frequency = str(check_data.get("frequency", "weekly"))
    _validate_frequency(check_frequency, "check.frequency")

    return AppConfig(
        backup=BackupRepositoryConfig(
            repository=repository,
            password_file=password_file,
            aws_access_key_id_file=_optional_path(backup_data, "aws_access_key_id_file"),
            aws_secret_access_key_file=_optional_path(backup_data, "aws_secret_access_key_file"),
        ),
        runtime=RuntimeConfig(
            worker_image=str(runtime_data.get("worker_image", "opigen-backup:latest")),
            worker_mounts=_parse_worker_mounts(runtime_data.get("worker_mounts", [])),
        ),
        state=StateConfig(path=Path(str(state_data.get("path", "/state/backup_state.json")))),
        health=HealthConfig(
            bind_host=str(health_data.get("bind_host", "127.0.0.1")),
            port=int(health_data.get("port", 8080)),
            readiness_max_age_seconds=_optional_int(
                health_data,
                "readiness_max_age_seconds",
                None,
            ),
        ),
        schedule=ScheduleConfig(
            enabled=bool(schedule_data.get("enabled", True)),
            frequency=schedule_frequency,
            preferred_time=_parse_time(str(schedule_data.get("preferred_time", "02:00:00"))),
        ),
        prune=PruneConfig(
            enabled=bool(prune_data.get("enabled", True)),
            keep_hourly=_optional_int(prune_data, "keep_hourly", 48),
            keep_daily=_optional_int(prune_data, "keep_daily", 14),
            keep_weekly=_optional_int(prune_data, "keep_weekly", 4),
            keep_monthly=_optional_int(prune_data, "keep_monthly", 12),
            keep_yearly=_optional_int(prune_data, "keep_yearly", 10),
        ),
        check=CheckConfig(
            enabled=bool(check_data.get("enabled", True)),
            frequency=check_frequency,
        ),
        restic=ResticConfig(
            global_args=tuple(shlex.split(str(restic_data.get("global_args", "")))),
        ),
        timeouts=TimeoutConfig(
            stop_grace_period=int(timeouts_data.get("stop_grace_period", 30)),
            backup_operation=int(timeouts_data.get("backup_operation", 3600)),
        ),
    )


def _table(data: dict[str, Any], name: str, *, required: bool = False) -> dict[str, Any]:
    value = data.get(name)
    if value is None:
        if required:
            raise ConfigError(f"Missing required [{name}] section")
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a TOML table")
    return value


def _required_str(data: dict[str, Any], key: str, section: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"[{section}] {key} is required")
    return value


def _optional_path(data: dict[str, Any], key: str) -> Path | None:
    value = data.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be a path string")
    return Path(value)


def _optional_int(data: dict[str, Any], key: str, default: int | None) -> int | None:
    value = data.get(key, default)
    if value is None:
        return None
    return int(value)


def _parse_worker_mounts(value: Any) -> tuple[WorkerMount, ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, list):
        raise ConfigError("runtime.worker_mounts must be a list of mount strings")

    mounts: list[WorkerMount] = []
    for item in value:
        if not isinstance(item, str):
            raise ConfigError("runtime.worker_mounts entries must be strings")
        parts = item.split(":")
        if len(parts) not in (2, 3) or not parts[0] or not parts[1]:
            raise ConfigError(
                "runtime.worker_mounts entries must use source:target or source:target:mode"
            )
        mode = parts[2] if len(parts) == 3 else "rw"
        if mode not in {"ro", "rw"}:
            raise ConfigError("runtime.worker_mounts mode must be ro or rw")
        mounts.append(WorkerMount(source=parts[0], target=parts[1], mode=mode))
    return tuple(mounts)


def _parse_time(value: str) -> time:
    parts = value.split(":")
    if len(parts) not in (2, 3):
        raise ConfigError("preferred_time must use HH:MM or HH:MM:SS")
    hour, minute, *rest = [int(part) for part in parts]
    second = rest[0] if rest else 0
    return time(hour=hour, minute=minute, second=second)


def _validate_frequency(value: str, field: str) -> None:
    if value not in {"hourly", "daily", "weekly"}:
        raise ConfigError(f"{field} must be one of hourly, daily, weekly")
