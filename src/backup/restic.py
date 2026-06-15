from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from backup.config import AppConfig, WorkerMount
from backup.docker_client import DockerClient, DockerContainer, WorkerResult
from backup.state import BackupState, StateStore, utc_now


class ResticError(RuntimeError):
    """Raised when restic cannot complete a required operation."""


@dataclass(frozen=True)
class ResticCommandResult:
    command: tuple[str, ...]
    exit_code: int
    output: str


class ResticRunner:
    def __init__(self, config: AppConfig, docker_client: DockerClient):
        self.config = config
        self.docker_client = docker_client
        self._environment: dict[str, str] | None = None
        self._repository_ready = False

    def backup_volume(
        self,
        container: DockerContainer,
        destination: str,
        extra_args: tuple[str, ...],
    ) -> ResticCommandResult:
        self.ensure_repository()
        command = ["restic", "backup", *self.config.restic.global_args, *extra_args]
        if "--json" not in command:
            command.append("--json")
        command.append(destination)
        return self._run(command, source_container=container)

    def restore_volume(
        self,
        *,
        snapshot_id: str,
        snapshot_path: str,
        target_volume: str,
    ) -> ResticCommandResult:
        self.ensure_repository()
        command = [
            "restic",
            "restore",
            *self.config.restic.global_args,
            snapshot_id,
            "--target",
            "/",
            "--include",
            snapshot_path,
        ]
        return self._run(
            command,
            worker_mounts=(WorkerMount(source=target_volume, target=snapshot_path, mode="rw"),),
        )

    def run_maintenance(
        self,
        state_store: StateStore,
        *,
        record_result: Callable[[str, bool], None] | None = None,
    ) -> None:
        state = state_store.load()
        changed = False
        now = utc_now()

        changed = self._run_prune_if_due(state, now, record_result) or changed
        changed = self._run_check_if_due(state, now, record_result) or changed

        if changed:
            state_store.save(state)

    def _run_prune_if_due(
        self,
        state: BackupState,
        now: datetime,
        record_result: Callable[[str, bool], None] | None,
    ) -> bool:
        if not (self.config.prune.enabled and _due(state.last_prune_at, "daily", now)):
            return False
        self.ensure_repository()
        result = self.forget_prune()
        if result.exit_code != 0:
            _record_maintenance_result(record_result, "prune", False)
            raise ResticError(f"restic forget --prune failed: {result.output}")
        state.last_prune_at = now
        state.runtime.last_prune_at = now
        state.runtime.last_prune_success = True
        _record_maintenance_result(record_result, "prune", True)
        return True

    def _run_check_if_due(
        self,
        state: BackupState,
        now: datetime,
        record_result: Callable[[str, bool], None] | None,
    ) -> bool:
        if not (
            self.config.check.enabled
            and _due(state.last_check_at, self.config.check.frequency, now)
        ):
            return False
        self.ensure_repository()
        result = self.check()
        if result.exit_code != 0:
            _record_maintenance_result(record_result, "check", False)
            raise ResticError(f"restic check failed: {result.output}")
        state.last_check_at = now
        state.runtime.last_check_at = now
        state.runtime.last_check_success = True
        _record_maintenance_result(record_result, "check", True)
        return True

    def ensure_repository(self) -> None:
        if self._repository_ready:
            return
        snapshots = self.snapshots()
        if snapshots.exit_code == 0:
            self._repository_ready = True
            return
        if not repository_needs_init(snapshots.output):
            raise ResticError(f"restic snapshots failed: {snapshots.output}")
        init = self.init_repository()
        if init.exit_code != 0:
            raise ResticError(f"restic init failed: {init.output}")
        self._repository_ready = True

    def snapshots(self) -> ResticCommandResult:
        return self._run(["restic", "snapshots", *self.config.restic.global_args])

    def init_repository(self) -> ResticCommandResult:
        return self._run(["restic", "init", *self.config.restic.global_args])

    def forget_prune(self) -> ResticCommandResult:
        command = ["restic", "forget", *self.config.restic.global_args, "--prune"]
        keep_options = {
            "--keep-hourly": self.config.prune.keep_hourly,
            "--keep-daily": self.config.prune.keep_daily,
            "--keep-weekly": self.config.prune.keep_weekly,
            "--keep-monthly": self.config.prune.keep_monthly,
            "--keep-yearly": self.config.prune.keep_yearly,
        }
        for option, value in keep_options.items():
            if value is not None:
                command.extend([option, str(value)])
        return self._run(command)

    def check(self) -> ResticCommandResult:
        return self._run(["restic", "check", *self.config.restic.global_args])

    def _run(
        self,
        command: list[str],
        *,
        source_container: DockerContainer | None = None,
        worker_mounts: tuple[WorkerMount, ...] = (),
    ) -> ResticCommandResult:
        worker_result: WorkerResult = self.docker_client.run_worker(
            image=self.config.runtime.worker_image,
            command=command,
            environment=self.environment(),
            timeout=self.config.timeouts.backup_operation,
            source_container=source_container,
            worker_mounts=(*self.config.runtime.worker_mounts, *worker_mounts),
        )
        return ResticCommandResult(
            command=tuple(command),
            exit_code=worker_result.exit_code,
            output=worker_result.output,
        )

    def environment(self) -> dict[str, str]:
        if self._environment is None:
            backup = self.config.backup
            env = {
                "HOME": "/tmp",  # nosec B108: restic worker tmpfs mount.
                "RESTIC_REPOSITORY": backup.repository,
                "RESTIC_PASSWORD": _read_secret(backup.password_file),
                "TMPDIR": "/tmp",  # nosec B108: restic worker tmpfs mount.
                "XDG_CACHE_HOME": "/tmp/restic-cache",  # nosec B108: restic worker tmpfs mount.
            }
            if backup.aws_access_key_id_file is not None:
                env["AWS_ACCESS_KEY_ID"] = _read_secret(backup.aws_access_key_id_file)
            if backup.aws_secret_access_key_file is not None:
                env["AWS_SECRET_ACCESS_KEY"] = _read_secret(backup.aws_secret_access_key_file)
            self._environment = env
        return dict(self._environment)


def repository_needs_init(output: str) -> bool:
    normalized = output.lower()
    markers = (
        "is there a repository at the following location",
        "unable to open config file",
        "config file does not exist",
        "repository does not exist",
        "not initialized",
    )
    return any(marker in normalized for marker in markers)


def parse_backup_snapshot_id(output: str) -> str | None:
    for line in output.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if item.get("message_type") == "summary" and item.get("snapshot_id"):
            return str(item["snapshot_id"])

    match = re.search(r"snapshot\s+([0-9a-fA-F]+)\s+saved", output)
    if match:
        return match.group(1)
    return None


def _read_secret(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def _record_maintenance_result(
    record_result: Callable[[str, bool], None] | None,
    operation: str,
    success: bool,
) -> None:
    if record_result is not None:
        record_result(operation, success)


def _due(last_run: datetime | None, frequency: str, now: datetime) -> bool:
    if last_run is None:
        return True
    intervals = {
        "hourly": timedelta(hours=1),
        "daily": timedelta(days=1),
        "weekly": timedelta(weeks=1),
    }
    return now - last_run >= intervals[frequency]
