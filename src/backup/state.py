from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path


@dataclass
class BackupRecord:
    id: str
    group: str
    container_name: str
    container_id: str
    volume_name: str
    volume_destination: str
    image_reference: str
    image_id: str | None
    repo_digest: str | None
    started_at: datetime
    completed_at: datetime
    outcome: str
    snapshot_id: str | None = None
    snapshot_paths: tuple[str, ...] = ()
    error: str | None = None


@dataclass
class RuntimeStatus:
    last_run_started_at: datetime | None = None
    last_run_completed_at: datetime | None = None
    last_success_at: datetime | None = None
    last_run_success: bool | None = None
    last_run_duration_seconds: float | None = None
    last_run_containers_attempted: int = 0
    last_run_volumes_attempted: int = 0
    last_error: str | None = None
    next_run_at: datetime | None = None
    last_prune_at: datetime | None = None
    last_prune_success: bool | None = None
    last_check_at: datetime | None = None
    last_check_success: bool | None = None


@dataclass
class HistogramState:
    buckets: dict[str, int] = field(default_factory=dict)
    count: int = 0
    total: float = 0.0


@dataclass
class MetricsState:
    backup_runs: dict[str, int] = field(default_factory=lambda: {"success": 0, "failure": 0})
    backup_volumes: dict[str, int] = field(default_factory=lambda: {"success": 0, "failure": 0})
    backup_containers: dict[str, int] = field(default_factory=lambda: {"success": 0, "failure": 0})
    restic_maintenance: dict[str, dict[str, int]] = field(
        default_factory=lambda: {
            "check": {"success": 0, "failure": 0},
            "prune": {"success": 0, "failure": 0},
        }
    )
    backup_run_duration: HistogramState = field(default_factory=HistogramState)
    backup_volume_duration: HistogramState = field(default_factory=HistogramState)


@dataclass
class BackupState:
    last_prune_at: datetime | None = None
    last_check_at: datetime | None = None
    backups: list[BackupRecord] = field(default_factory=list)
    runtime: RuntimeStatus = field(default_factory=RuntimeStatus)
    metrics: MetricsState = field(default_factory=MetricsState)


class StateStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> BackupState:
        if not self.path.exists():
            return BackupState()
        data = json.loads(self.path.read_text(encoding="utf-8"))
        runtime_data = data.get("runtime", {})
        if not isinstance(runtime_data, dict):
            runtime_data = {}
        metrics_data = data.get("metrics", {})
        if not isinstance(metrics_data, dict):
            metrics_data = {}
        return BackupState(
            last_prune_at=_parse_datetime(data.get("last_prune_at")),
            last_check_at=_parse_datetime(data.get("last_check_at")),
            backups=[
                _parse_backup_record(item)
                for item in data.get("backups", [])
                if isinstance(item, dict)
            ],
            runtime=_parse_runtime_status(
                runtime_data,
                legacy_last_prune_at=_parse_datetime(data.get("last_prune_at")),
                legacy_last_check_at=_parse_datetime(data.get("last_check_at")),
            ),
            metrics=_parse_metrics_state(metrics_data),
        )

    def save(self, state: BackupState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "last_prune_at": _format_datetime(state.last_prune_at),
            "last_check_at": _format_datetime(state.last_check_at),
            "backups": [_backup_record_payload(record) for record in state.backups],
            "runtime": _runtime_status_payload(state.runtime),
            "metrics": _metrics_state_payload(state.metrics),
        }
        tmp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        tmp_path.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")
        tmp_path.replace(self.path)

    def update(self, mutator: Callable[[BackupState], None]) -> BackupState:
        state = self.load()
        mutator(state)
        self.save(state)
        return state


def utc_now() -> datetime:
    return datetime.now(UTC)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _format_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat()


def _parse_backup_record(data: dict) -> BackupRecord:
    return BackupRecord(
        id=str(data.get("id", "")),
        group=str(data.get("group", "")),
        container_name=str(data.get("container_name", "")),
        container_id=str(data.get("container_id", "")),
        volume_name=str(data.get("volume_name", "")),
        volume_destination=str(data.get("volume_destination", "")),
        image_reference=str(data.get("image_reference", "")),
        image_id=_optional_str(data.get("image_id")),
        repo_digest=_optional_str(data.get("repo_digest")),
        started_at=_parse_datetime(data.get("started_at")) or utc_now(),
        completed_at=_parse_datetime(data.get("completed_at")) or utc_now(),
        outcome=str(data.get("outcome", "failure")),
        snapshot_id=_optional_str(data.get("snapshot_id")),
        snapshot_paths=tuple(str(path) for path in data.get("snapshot_paths", [])),
        error=_optional_str(data.get("error")),
    )


def _backup_record_payload(record: BackupRecord) -> dict[str, object]:
    return {
        "id": record.id,
        "group": record.group,
        "container_name": record.container_name,
        "container_id": record.container_id,
        "volume_name": record.volume_name,
        "volume_destination": record.volume_destination,
        "image_reference": record.image_reference,
        "image_id": record.image_id,
        "repo_digest": record.repo_digest,
        "started_at": _format_datetime(record.started_at),
        "completed_at": _format_datetime(record.completed_at),
        "outcome": record.outcome,
        "snapshot_id": record.snapshot_id,
        "snapshot_paths": list(record.snapshot_paths),
        "error": record.error,
    }


def _parse_runtime_status(
    data: dict,
    *,
    legacy_last_prune_at: datetime | None,
    legacy_last_check_at: datetime | None,
) -> RuntimeStatus:
    return RuntimeStatus(
        last_run_started_at=_parse_datetime(data.get("last_run_started_at")),
        last_run_completed_at=_parse_datetime(data.get("last_run_completed_at")),
        last_success_at=_parse_datetime(data.get("last_success_at")),
        last_run_success=_optional_bool(data.get("last_run_success")),
        last_run_duration_seconds=_optional_float(data.get("last_run_duration_seconds")),
        last_run_containers_attempted=int(data.get("last_run_containers_attempted", 0)),
        last_run_volumes_attempted=int(data.get("last_run_volumes_attempted", 0)),
        last_error=_optional_str(data.get("last_error")),
        next_run_at=_parse_datetime(data.get("next_run_at")),
        last_prune_at=_parse_datetime(data.get("last_prune_at")) or legacy_last_prune_at,
        last_prune_success=_optional_bool(data.get("last_prune_success")),
        last_check_at=_parse_datetime(data.get("last_check_at")) or legacy_last_check_at,
        last_check_success=_optional_bool(data.get("last_check_success")),
    )


def _runtime_status_payload(status: RuntimeStatus) -> dict[str, object]:
    return {
        "last_run_started_at": _format_datetime(status.last_run_started_at),
        "last_run_completed_at": _format_datetime(status.last_run_completed_at),
        "last_success_at": _format_datetime(status.last_success_at),
        "last_run_success": status.last_run_success,
        "last_run_duration_seconds": status.last_run_duration_seconds,
        "last_run_containers_attempted": status.last_run_containers_attempted,
        "last_run_volumes_attempted": status.last_run_volumes_attempted,
        "last_error": status.last_error,
        "next_run_at": _format_datetime(status.next_run_at),
        "last_prune_at": _format_datetime(status.last_prune_at),
        "last_prune_success": status.last_prune_success,
        "last_check_at": _format_datetime(status.last_check_at),
        "last_check_success": status.last_check_success,
    }


def _parse_metrics_state(data: dict) -> MetricsState:
    return MetricsState(
        backup_runs=_status_counts(data.get("backup_runs")),
        backup_volumes=_status_counts(data.get("backup_volumes")),
        backup_containers=_status_counts(data.get("backup_containers")),
        restic_maintenance=_maintenance_counts(data.get("restic_maintenance")),
        backup_run_duration=_parse_histogram(data.get("backup_run_duration")),
        backup_volume_duration=_parse_histogram(data.get("backup_volume_duration")),
    )


def _metrics_state_payload(metrics: MetricsState) -> dict[str, object]:
    return {
        "backup_runs": dict(metrics.backup_runs),
        "backup_volumes": dict(metrics.backup_volumes),
        "backup_containers": dict(metrics.backup_containers),
        "restic_maintenance": {
            operation: dict(counts) for operation, counts in metrics.restic_maintenance.items()
        },
        "backup_run_duration": _histogram_payload(metrics.backup_run_duration),
        "backup_volume_duration": _histogram_payload(metrics.backup_volume_duration),
    }


def _parse_histogram(value: object) -> HistogramState:
    if not isinstance(value, dict):
        return HistogramState()
    buckets = value.get("buckets", {})
    return HistogramState(
        buckets={str(bucket): int(count) for bucket, count in buckets.items()}
        if isinstance(buckets, dict)
        else {},
        count=int(value.get("count", 0)),
        total=float(value.get("total", 0.0)),
    )


def _histogram_payload(histogram: HistogramState) -> dict[str, object]:
    return {
        "buckets": dict(histogram.buckets),
        "count": histogram.count,
        "total": histogram.total,
    }


def _status_counts(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {"success": 0, "failure": 0}
    return {
        "success": int(value.get("success", 0)),
        "failure": int(value.get("failure", 0)),
    }


def _maintenance_counts(value: object) -> dict[str, dict[str, int]]:
    counts = {
        "check": {"success": 0, "failure": 0},
        "prune": {"success": 0, "failure": 0},
    }
    if not isinstance(value, dict):
        return counts
    for operation in counts:
        counts[operation] = _status_counts(value.get(operation))
    return counts


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def _optional_bool(value: object) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return str(value).lower() == "true"


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    return float(value)
