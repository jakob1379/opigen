from __future__ import annotations

import logging
import re
import time
from collections import defaultdict
from dataclasses import dataclass

from backup.config import AppConfig
from backup.docker_client import DockerClient, DockerContainer
from backup.labels import ContainerBackupLabels, LabelError, parse_container_labels
from backup.metrics import (
    record_backup_container,
    record_backup_run,
    record_backup_volume,
    record_restic_maintenance,
)
from backup.restic import ResticRunner, parse_backup_snapshot_id
from backup.state import BackupRecord, StateStore, utc_now
from backup.volumes import VolumeSelectionError, select_named_volumes

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class BackupMember:
    container: DockerContainer
    labels: ContainerBackupLabels


@dataclass
class BackupRunContext:
    containers_attempted: int = 0
    volumes_attempted: int = 0
    last_error: str | None = None


class BackupOrchestrator:
    def __init__(
        self,
        config: AppConfig,
        docker_client: DockerClient,
        restic_runner: ResticRunner,
        state_store: StateStore,
    ):
        self.config = config
        self.docker_client = docker_client
        self.restic_runner = restic_runner
        self.state_store = state_store

    def run_once(self) -> bool:
        started_at = utc_now()
        monotonic_started = time.perf_counter()
        context = BackupRunContext()
        self.state_store.update(
            lambda state: _mark_run_started(state, started_at),
        )

        groups = self.discover_groups()
        success = True
        for group_name in sorted(groups):
            group_success = self._process_group(group_name, groups[group_name], context)
            success = success and group_success

        maintenance_results: list[tuple[str, bool]] = []
        try:
            self.restic_runner.run_maintenance(
                self.state_store,
                record_result=lambda operation, ok: maintenance_results.append((operation, ok)),
            )
        except Exception as exc:
            LOGGER.exception("maintenance_failed")
            context.last_error = _error_summary(exc)
            success = False
        finally:
            completed_at = utc_now()
            duration = time.perf_counter() - monotonic_started
            self.state_store.update(
                lambda state: _mark_run_finished(
                    state,
                    completed_at=completed_at,
                    success=success,
                    duration_seconds=duration,
                    context=context,
                    maintenance_results=maintenance_results,
                ),
            )
        return success

    def discover_groups(self) -> dict[str, list[BackupMember]]:
        groups: dict[str, list[BackupMember]] = defaultdict(list)
        for container in self.docker_client.list_backup_containers():
            try:
                labels = parse_container_labels(container.labels)
            except LabelError as exc:
                LOGGER.error(
                    "container_config_error",
                    extra={"container": container.name, "error": str(exc)},
                )
                continue
            groups[labels.group].append(BackupMember(container=container, labels=labels))
        return dict(groups)

    def _process_group(
        self,
        group_name: str,
        members: list[BackupMember],
        context: BackupRunContext,
    ) -> bool:
        LOGGER.info("group_started", extra={"group": group_name, "containers": len(members)})
        stopped_by_run: list[DockerContainer] = []
        group_success = True

        for member in members:
            if member.labels.stop and member.container.running:
                try:
                    member.container.stop(timeout=self.config.timeouts.stop_grace_period)
                    stopped_by_run.append(member.container)
                    LOGGER.info(
                        "container_stopped",
                        extra={"group": group_name, "container": member.container.name},
                    )
                except Exception:
                    LOGGER.exception(
                        "container_stop_failed",
                        extra={"group": group_name, "container": member.container.name},
                    )
                    context.last_error = f"failed to stop container {member.container.name}"
                    self._restart_stopped(group_name, stopped_by_run)
                    return False

        try:
            for member in members:
                if not self._backup_container(group_name, member, context):
                    group_success = False
        finally:
            if not self._restart_stopped(group_name, stopped_by_run):
                group_success = False

        LOGGER.info("group_finished", extra={"group": group_name, "success": group_success})
        return group_success

    def _backup_container(
        self,
        group_name: str,
        member: BackupMember,
        context: BackupRunContext,
    ) -> bool:
        container = member.container
        context.containers_attempted += 1
        try:
            volumes = select_named_volumes(container.mounts, member.labels.volumes)
        except VolumeSelectionError as exc:
            LOGGER.error(
                "volume_selection_failed",
                extra={"group": group_name, "container": container.name, "error": str(exc)},
            )
            context.last_error = str(exc)
            self.state_store.update(
                lambda state: record_backup_container(state, success=False),
            )
            return False

        if not volumes:
            LOGGER.warning(
                "container_has_no_named_volumes",
                extra={"group": group_name, "container": container.name},
            )
            self.state_store.update(
                lambda state: record_backup_container(state, success=True),
            )
            return True

        container_success = True
        for volume in volumes:
            context.volumes_attempted += 1
            backup_started_at = utc_now()
            volume_started = time.perf_counter()
            result = self.restic_runner.backup_volume(
                container,
                volume.destination,
                member.labels.restic_args,
            )
            backup_completed_at = utc_now()
            volume_duration = time.perf_counter() - volume_started
            if result.exit_code != 0:
                context.last_error = _error_summary(result.output)
                LOGGER.error(
                    "backup_failed",
                    extra={
                        "group": group_name,
                        "container": container.name,
                        "volume": volume.name,
                        "destination": volume.destination,
                        "exit_code": result.exit_code,
                        "output": result.output,
                    },
                )
                self._record_backup_volume(
                    BackupRecord(
                        id=_backup_record_id(
                            group_name,
                            container.name,
                            volume.name or "volume",
                            backup_completed_at,
                        ),
                        group=group_name,
                        container_name=container.name,
                        container_id=str(container.id),
                        volume_name=volume.name or "",
                        volume_destination=volume.destination,
                        image_reference=container.image.reference,
                        image_id=container.image.image_id,
                        repo_digest=_first_repo_digest(container),
                        started_at=backup_started_at,
                        completed_at=backup_completed_at,
                        outcome="failure",
                        snapshot_paths=(volume.destination,),
                        error=_error_summary(result.output),
                    ),
                    success=False,
                    duration_seconds=volume_duration,
                )
                container_success = False
                continue
            snapshot_id = parse_backup_snapshot_id(result.output)
            self._record_backup_volume(
                BackupRecord(
                    id=_backup_record_id(
                        group_name,
                        container.name,
                        volume.name or "volume",
                        backup_completed_at,
                    ),
                    group=group_name,
                    container_name=container.name,
                    container_id=str(container.id),
                    volume_name=volume.name or "",
                    volume_destination=volume.destination,
                    image_reference=container.image.reference,
                    image_id=container.image.image_id,
                    repo_digest=_first_repo_digest(container),
                    started_at=backup_started_at,
                    completed_at=backup_completed_at,
                    outcome="success",
                    snapshot_id=snapshot_id,
                    snapshot_paths=(volume.destination,),
                ),
                success=True,
                duration_seconds=volume_duration,
            )
            LOGGER.info(
                "backup_finished",
                extra={
                    "group": group_name,
                    "container": container.name,
                    "volume": volume.name,
                    "destination": volume.destination,
                },
            )
        self.state_store.update(
            lambda state: record_backup_container(state, success=container_success),
        )
        return container_success

    def _record_backup_volume(
        self,
        record: BackupRecord,
        *,
        success: bool,
        duration_seconds: float,
    ) -> None:
        def update(state):
            state.backups.append(record)
            record_backup_volume(state, success=success, duration_seconds=duration_seconds)

        self.state_store.update(update)

    def _restart_stopped(self, group_name: str, stopped_by_run: list[DockerContainer]) -> bool:
        success = True
        for container in reversed(stopped_by_run):
            try:
                container.start()
                LOGGER.info(
                    "container_started",
                    extra={"group": group_name, "container": container.name},
                )
            except Exception:
                LOGGER.exception(
                    "container_start_failed",
                    extra={"group": group_name, "container": container.name},
                )
                success = False
        return success


def _mark_run_started(state, started_at) -> None:
    state.runtime.last_run_started_at = started_at
    state.runtime.last_run_completed_at = None
    state.runtime.last_run_success = None
    state.runtime.last_run_duration_seconds = None
    state.runtime.last_run_containers_attempted = 0
    state.runtime.last_run_volumes_attempted = 0
    state.runtime.last_error = None


def _mark_run_finished(
    state,
    *,
    completed_at,
    success: bool,
    duration_seconds: float,
    context: BackupRunContext,
    maintenance_results: list[tuple[str, bool]],
) -> None:
    state.runtime.last_run_completed_at = completed_at
    state.runtime.last_run_success = success
    state.runtime.last_run_duration_seconds = duration_seconds
    state.runtime.last_run_containers_attempted = context.containers_attempted
    state.runtime.last_run_volumes_attempted = context.volumes_attempted
    state.runtime.last_error = context.last_error
    if success:
        state.runtime.last_success_at = completed_at
    for operation, operation_success in maintenance_results:
        record_restic_maintenance(state, operation=operation, success=operation_success)
        if operation == "prune":
            state.runtime.last_prune_at = completed_at
            state.runtime.last_prune_success = operation_success
        if operation == "check":
            state.runtime.last_check_at = completed_at
            state.runtime.last_check_success = operation_success
    record_backup_run(state, success=success, duration_seconds=duration_seconds)


def _backup_record_id(
    group_name: str,
    container_name: str,
    volume_name: str,
    completed_at,
) -> str:
    base = f"{completed_at.strftime('%Y%m%d%H%M%S')}-{group_name}-{container_name}-{volume_name}"
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", base).strip("-")


def _first_repo_digest(container: DockerContainer) -> str | None:
    return next(iter(container.image.repo_digests), None)


def _error_summary(error: object) -> str:
    text = str(error).strip()
    return text.splitlines()[0][:500] if text else "unknown error"
