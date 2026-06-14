from __future__ import annotations

import shlex
from dataclasses import dataclass


class LabelError(ValueError):
    """Raised when backup labels are missing or malformed."""


@dataclass(frozen=True)
class VolumeSelection:
    all_volumes: bool = True
    selectors: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContainerBackupLabels:
    group: str
    stop: bool = True
    restic_args: tuple[str, ...] = ()
    volumes: VolumeSelection = VolumeSelection()


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
        restic_args=tuple(shlex.split(labels.get("backup.args", ""))),
        volumes=parse_volume_selection(labels.get("backup.volumes", "all")),
    )


def parse_volume_selection(value: str) -> VolumeSelection:
    raw_value = value.strip()
    if not raw_value or raw_value.lower() == "all":
        return VolumeSelection(all_volumes=True)

    selectors = tuple(item.strip() for item in raw_value.split(",") if item.strip())
    if not selectors:
        raise LabelError("backup.volumes must be all or a comma-separated selector list")
    return VolumeSelection(all_volumes=False, selectors=selectors)


def _parse_bool(value: str, label: str) -> bool:
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise LabelError(f"{label} must be true or false")
