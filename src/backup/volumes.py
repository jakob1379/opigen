from __future__ import annotations

from dataclasses import dataclass

from backup.config import DiscoveryConfig, WorkerMount
from backup.docker_client import Mount
from backup.labels import MountSelection


class MountSelectionError(ValueError):
    """Raised when a container's requested backup mounts cannot be resolved."""


@dataclass(frozen=True)
class BackupMount:
    mount: Mount
    worker_mounts: tuple[WorkerMount, ...] = ()
    skip_reason: str | None = None

    @property
    def selected(self) -> bool:
        return self.skip_reason is None

    @property
    def display_name(self) -> str:
        return self.mount.name or self.mount.source or self.mount.destination


def select_backup_mounts(
    mounts: list[Mount],
    selection: MountSelection | None,
    discovery: DiscoveryConfig,
) -> list[BackupMount]:
    candidates = [_backup_mount_candidate(mount) for mount in mounts]
    if selection is None and discovery.default_mounts == "named":
        return [
            candidate
            for candidate in candidates
            if candidate.selected and candidate.mount.type == "volume"
        ]
    effective_selection = selection or _default_selection(discovery)
    if effective_selection.all_mounts:
        return candidates

    selected: list[BackupMount] = []
    missing: list[str] = []
    for selector in effective_selection.selectors:
        match = next(
            (
                candidate
                for candidate in candidates
                if candidate.selected
                and (candidate.mount.name == selector or candidate.mount.destination == selector)
            ),
            None,
        )
        if match is None:
            missing.append(selector)
        elif match not in selected:
            selected.append(match)

    if missing:
        joined = ", ".join(missing)
        raise MountSelectionError(f"Selectors did not match backup-capable mounts: {joined}")
    return selected


def _default_selection(discovery: DiscoveryConfig) -> MountSelection:
    if discovery.default_mounts == "all":
        return MountSelection(all_mounts=True)
    return MountSelection(all_mounts=False, selectors=())


def _backup_mount_candidate(mount: Mount) -> BackupMount:
    if mount.type == "volume" and mount.name:
        return BackupMount(mount=mount)
    if mount.type == "bind":
        if not mount.source:
            return BackupMount(mount=mount, skip_reason="bind mount has no source path")
        return BackupMount(
            mount=mount,
            worker_mounts=(WorkerMount(source=mount.source, target=mount.destination, mode="ro"),),
        )
    return BackupMount(
        mount=mount,
        skip_reason=f"unsupported mount type: {mount.type or 'unknown'}",
    )
