from __future__ import annotations

from backup.docker_client import Mount
from backup.labels import VolumeSelection


class VolumeSelectionError(ValueError):
    """Raised when a container's requested backup volumes cannot be resolved."""


def select_named_volumes(mounts: list[Mount], selection: VolumeSelection) -> list[Mount]:
    eligible = [mount for mount in mounts if mount.type == "volume" and mount.name]
    if selection.all_volumes:
        return eligible

    selected: list[Mount] = []
    missing: list[str] = []
    for selector in selection.selectors:
        match = next(
            (
                mount
                for mount in eligible
                if mount.name == selector or mount.destination == selector
            ),
            None,
        )
        if match is None:
            missing.append(selector)
        elif match not in selected:
            selected.append(match)

    if missing:
        joined = ", ".join(missing)
        raise VolumeSelectionError(f"Selectors did not match named volumes: {joined}")
    return selected
