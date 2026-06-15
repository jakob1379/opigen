from __future__ import annotations

import shlex
from dataclasses import dataclass


class LabelError(ValueError):
    """Raised when backup labels are missing or malformed."""


@dataclass(frozen=True)
class MountSelection:
    all_mounts: bool = True
    selectors: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContainerBackupLabels:
    group: str
    stop: bool = True
    restic_args: tuple[str, ...] = ()
    mounts: MountSelection | None = None
    pre_stop_signal: str | None = None
    pre_stop_wait_seconds: int = 0


def is_backup_enabled(labels: dict[str, str]) -> bool:
    return labels.get("backup.enabled", "").lower() == "true"


def parse_container_labels(labels: dict[str, str]) -> ContainerBackupLabels:
    if not is_backup_enabled(labels):
        raise LabelError("backup.enabled must be true")

    group = labels.get("backup.group", "").strip()
    if not group:
        raise LabelError("backup.group is required")

    return ContainerBackupLabels(
        group=group,
        stop=_parse_bool(labels.get("backup.stop", "true"), "backup.stop"),
        restic_args=tuple(shlex.split(labels.get("backup.restic_args", ""))),
        mounts=(
            parse_mount_selection(labels["backup.mounts"]) if "backup.mounts" in labels else None
        ),
        pre_stop_signal=_optional_signal(labels.get("backup.pre_stop_signal")),
        pre_stop_wait_seconds=_parse_non_negative_int(
            labels.get("backup.pre_stop_wait_seconds", "0"),
            "backup.pre_stop_wait_seconds",
        ),
    )


def parse_mount_selection(value: str) -> MountSelection:
    raw_value = value.strip()
    if not raw_value or raw_value.lower() == "all":
        return MountSelection(all_mounts=True)

    selectors = tuple(item.strip() for item in raw_value.split(",") if item.strip())
    if not selectors:
        raise LabelError("backup.mounts must be all or a comma-separated selector list")
    return MountSelection(all_mounts=False, selectors=selectors)


def _parse_bool(value: str, label: str) -> bool:
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise LabelError(f"{label} must be true or false")


def _optional_signal(value: str | None) -> str | None:
    if value is None:
        return None
    signal = value.strip()
    return signal or None


def _parse_non_negative_int(value: str, label: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise LabelError(f"{label} must be a non-negative integer") from exc
    if parsed < 0:
        raise LabelError(f"{label} must be a non-negative integer")
    return parsed
