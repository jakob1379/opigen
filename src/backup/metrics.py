from __future__ import annotations

from dataclasses import dataclass
from importlib import metadata

from backup.config import AppConfig
from backup.state import BackupState, HistogramState

DEFAULT_BUCKETS = (1.0, 5.0, 15.0, 60.0, 300.0, 900.0, 3600.0)


@dataclass(frozen=True)
class MetricObservation:
    name: str
    value: float


def record_backup_run(state: BackupState, *, success: bool, duration_seconds: float) -> None:
    state.metrics.backup_runs[_status(success)] += 1
    observe_histogram(state.metrics.backup_run_duration, duration_seconds)


def record_backup_volume(state: BackupState, *, success: bool, duration_seconds: float) -> None:
    state.metrics.backup_volumes[_status(success)] += 1
    observe_histogram(state.metrics.backup_volume_duration, duration_seconds)


def record_backup_container(state: BackupState, *, success: bool) -> None:
    state.metrics.backup_containers[_status(success)] += 1


def record_restic_maintenance(state: BackupState, *, operation: str, success: bool) -> None:
    state.metrics.restic_maintenance.setdefault(operation, {"success": 0, "failure": 0})
    state.metrics.restic_maintenance[operation][_status(success)] += 1


def observe_histogram(histogram: HistogramState, value: float) -> None:
    histogram.count += 1
    histogram.total += value
    for bucket in DEFAULT_BUCKETS:
        key = _bucket_key(bucket)
        histogram.buckets[key] = histogram.buckets.get(key, 0) + int(value <= bucket)
    histogram.buckets["+Inf"] = histogram.buckets.get("+Inf", 0) + 1


def render_prometheus(state: BackupState, config: AppConfig) -> str:
    lines: list[str] = []
    runtime = state.runtime

    _gauge(
        lines,
        "opigen_backup_last_success_timestamp_seconds",
        _timestamp(runtime.last_success_at),
    )
    _gauge(
        lines,
        "opigen_backup_last_run_timestamp_seconds",
        _timestamp(runtime.last_run_started_at),
    )
    _gauge(
        lines,
        "opigen_backup_last_run_success",
        1.0 if runtime.last_run_success else 0.0,
    )
    _gauge(
        lines,
        "opigen_backup_next_run_timestamp_seconds",
        _timestamp(runtime.next_run_at),
    )
    lines.append(
        'opigen_backup_info{version="'
        f'{_escape(_version())}",image="{_escape(config.runtime.worker_image)}'
        '"} 1'
    )

    _counter(lines, "opigen_backup_runs_total", state.metrics.backup_runs)
    _counter(lines, "opigen_backup_volumes_total", state.metrics.backup_volumes)
    _counter(lines, "opigen_backup_containers_total", state.metrics.backup_containers)
    for operation, counts in sorted(state.metrics.restic_maintenance.items()):
        for status, value in sorted(counts.items()):
            lines.append(
                "opigen_restic_maintenance_total"
                f'{{operation="{_escape(operation)}",status="{_escape(status)}"}} {value}'
            )

    _histogram(lines, "opigen_backup_run_duration_seconds", state.metrics.backup_run_duration)
    _histogram(
        lines,
        "opigen_backup_volume_duration_seconds",
        state.metrics.backup_volume_duration,
    )
    return "\n".join(lines) + "\n"


def _status(success: bool) -> str:
    return "success" if success else "failure"


def _gauge(lines: list[str], name: str, value: float) -> None:
    lines.append(f"{name} {value:g}")


def _counter(lines: list[str], name: str, counts: dict[str, int]) -> None:
    for status in ("failure", "success"):
        lines.append(f'{name}{{status="{status}"}} {counts.get(status, 0)}')


def _histogram(lines: list[str], name: str, histogram: HistogramState) -> None:
    for bucket in (*(_bucket_key(value) for value in DEFAULT_BUCKETS), "+Inf"):
        lines.append(f'{name}_bucket{{le="{bucket}"}} {histogram.buckets.get(bucket, 0)}')
    lines.append(f"{name}_count {histogram.count}")
    lines.append(f"{name}_sum {histogram.total:g}")


def _bucket_key(value: float) -> str:
    return f"{value:g}"


def _timestamp(value) -> float:
    if value is None:
        return 0.0
    return value.timestamp()


def _version() -> str:
    try:
        return metadata.version("opigen-backup")
    except metadata.PackageNotFoundError:
        return "0.1.0"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
