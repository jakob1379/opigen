from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass
class BackupState:
    last_prune_at: datetime | None = None
    last_check_at: datetime | None = None


class StateStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> BackupState:
        if not self.path.exists():
            return BackupState()
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return BackupState(
            last_prune_at=_parse_datetime(data.get("last_prune_at")),
            last_check_at=_parse_datetime(data.get("last_check_at")),
        )

    def save(self, state: BackupState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "last_prune_at": _format_datetime(state.last_prune_at),
            "last_check_at": _format_datetime(state.last_check_at),
        }
        tmp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        tmp_path.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")
        tmp_path.replace(self.path)


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
