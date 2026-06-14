from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from backup.config import AppConfig
from backup.docker_client import DockerClient, DockerContainer, WorkerResult
from backup.state import StateStore, utc_now


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
        command = ["restic", "backup", *self.config.restic.global_args, *extra_args, destination]
        return self._run(command, source_container=container)

    def run_maintenance(self, state_store: StateStore) -> None:
        state = state_store.load()
        changed = False
        now = utc_now()

        if self.config.prune.enabled and _due(state.last_prune_at, "daily", now):
            self.ensure_repository()
            result = self.forget_prune()
            if result.exit_code != 0:
                raise ResticError(f"restic forget --prune failed: {result.output}")
            state.last_prune_at = now
            changed = True

        check_due = _due(state.last_check_at, self.config.check.frequency, now)
        if self.config.check.enabled and check_due:
            self.ensure_repository()
            result = self.check()
            if result.exit_code != 0:
                raise ResticError(f"restic check failed: {result.output}")
            state.last_check_at = now
            changed = True

        if changed:
            state_store.save(state)

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
    ) -> ResticCommandResult:
        worker_result: WorkerResult = self.docker_client.run_worker(
            image=self.config.runtime.worker_image,
            command=command,
            environment=self.environment(),
            timeout=self.config.timeouts.backup_operation,
            source_container=source_container,
            worker_mounts=self.config.runtime.worker_mounts,
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
                "RESTIC_REPOSITORY": backup.repository,
                "RESTIC_PASSWORD": _read_secret(backup.password_file),
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


def _read_secret(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def _due(last_run: datetime | None, frequency: str, now: datetime) -> bool:
    if last_run is None:
        return True
    intervals = {
        "hourly": timedelta(hours=1),
        "daily": timedelta(days=1),
        "weekly": timedelta(weeks=1),
    }
    return now - last_run >= intervals[frequency]
