from __future__ import annotations

import re
import time
from collections import defaultdict
from dataclasses import dataclass

import structlog

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
from backup.volumes import BackupMount, MountSelectionError, select_backup_mounts

LOGGER = structlog.get_logger(__name__)


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

    def dry_run(self) -> bool:
        LOGGER.info("dry_run_started")
        groups = self.discover_groups()
        success = True
        for group_name in sorted(groups):
            if not self._dry_run_group(group_name, groups[group_name]):
                success = False
        planned_maintenance = self.restic_runner.planned_maintenance(self.state_store.load())
        LOGGER.info("dry_run_maintenance_planned", operations=list(planned_maintenance))
        LOGGER.info("dry_run_finished", success=success)
        return success

    def discover_groups(self) -> dict[str, list[BackupMember]]:
        groups: dict[str, list[BackupMember]] = defaultdict(list)
        for container in self.docker_client.list_backup_containers():
            try:
                labels = parse_container_labels(container.labels)
            except LabelError as exc:
                LOGGER.error(
                    "container_config_error",
                    container=container.name,
                    error=str(exc),
                )
                continue
            groups[labels.group].append(BackupMember(container=container, labels=labels))
        return dict(groups)

    def _dry_run_group(self, group_name: str, members: list[BackupMember]) -> bool:
        LOGGER.info("dry_run_group", group=group_name, containers=len(members))
        group_success = True
        for member in members:
            container = member.container
            if member.labels.stop and container.running:
                if member.labels.pre_stop_signal:
                    LOGGER.info(
                        "dry_run_pre_stop_signal_planned",
                        group=group_name,
                        container=container.name,
                        signal=member.labels.pre_stop_signal,
                        wait_seconds=member.labels.pre_stop_wait_seconds,
                    )
                LOGGER.info(
                    "dry_run_container_stop_planned",
                    group=group_name,
                    container=container.name,
                    timeout_seconds=self.config.timeouts.stop_grace_period,
                )
            if not self._dry_run_container(group_name, member):
                group_success = False
            if member.labels.stop and container.running:
                LOGGER.info(
                    "dry_run_container_start_planned",
                    group=group_name,
                    container=container.name,
                )
        return group_success

    def _dry_run_container(self, group_name: str, member: BackupMember) -> bool:
        try:
            mounts = select_backup_mounts(
                member.container.mounts,
                member.labels.mounts,
                self.config.discovery,
            )
        except MountSelectionError as exc:
            LOGGER.error(
                "dry_run_mount_selection_failed",
                group=group_name,
                container=member.container.name,
                error=str(exc),
            )
            return False

        selected = _log_mount_plan(group_name, member.container.name, mounts, dry_run=True)
        for backup_mount in selected:
            command = [
                "restic",
                "backup",
                *self.config.restic.global_args,
                *member.labels.restic_args,
                backup_mount.mount.destination,
            ]
            LOGGER.info(
                "dry_run_backup_planned",
                group=group_name,
                container=member.container.name,
                mount_type=backup_mount.mount.type,
                mount_name=backup_mount.mount.name,
                mount_source=backup_mount.mount.source,
                destination=backup_mount.mount.destination,
                worker_mounts=[
                    f"{mount.source}:{mount.target}:{mount.mode}"
                    for mount in backup_mount.worker_mounts
                ],
                command=command,
            )
        return True

    def _process_group(
        self,
        group_name: str,
        members: list[BackupMember],
        context: BackupRunContext,
    ) -> bool:
        LOGGER.info("group_started", group=group_name, containers=len(members))
        stopped_by_run: list[DockerContainer] = []
        group_success = True

        for member in members:
            if not self._stop_group_member(group_name, member, context, stopped_by_run):
                return False

        try:
            for member in members:
                if not self._backup_container(group_name, member, context):
                    group_success = False
        finally:
            if not self._restart_stopped(group_name, stopped_by_run):
                group_success = False

        LOGGER.info("group_finished", group=group_name, success=group_success)
        return group_success

    def _stop_group_member(
        self,
        group_name: str,
        member: BackupMember,
        context: BackupRunContext,
        stopped_by_run: list[DockerContainer],
    ) -> bool:
        if not (member.labels.stop and member.container.running):
            return True
        if member.labels.pre_stop_signal and not self._send_pre_stop_signal(
            group_name,
            member,
            context,
            stopped_by_run,
        ):
            return False
        try:
            member.container.stop(timeout=self.config.timeouts.stop_grace_period)
            stopped_by_run.append(member.container)
            LOGGER.info(
                "container_stopped",
                group=group_name,
                container=member.container.name,
            )
            return True
        except Exception:
            LOGGER.exception(
                "container_stop_failed",
                group=group_name,
                container=member.container.name,
            )
            context.last_error = f"failed to stop container {member.container.name}"
            self._restart_stopped(group_name, stopped_by_run)
            return False

    def _send_pre_stop_signal(
        self,
        group_name: str,
        member: BackupMember,
        context: BackupRunContext,
        stopped_by_run: list[DockerContainer],
    ) -> bool:
        try:
            member.container.signal(member.labels.pre_stop_signal or "")
            LOGGER.info(
                "container_pre_stop_signaled",
                group=group_name,
                container=member.container.name,
                signal=member.labels.pre_stop_signal,
                wait_seconds=member.labels.pre_stop_wait_seconds,
            )
            if member.labels.pre_stop_wait_seconds:
                time.sleep(member.labels.pre_stop_wait_seconds)
            return True
        except Exception:
            LOGGER.exception(
                "container_pre_stop_signal_failed",
                group=group_name,
                container=member.container.name,
                signal=member.labels.pre_stop_signal,
            )
            context.last_error = f"failed to signal container {member.container.name}"
            self._restart_stopped(group_name, stopped_by_run)
            return False

    def _backup_container(
        self,
        group_name: str,
        member: BackupMember,
        context: BackupRunContext,
    ) -> bool:
        container = member.container
        context.containers_attempted += 1
        try:
            mounts = select_backup_mounts(
                container.mounts,
                member.labels.mounts,
                self.config.discovery,
            )
        except MountSelectionError as exc:
            LOGGER.error(
                "mount_selection_failed",
                group=group_name,
                container=container.name,
                error=str(exc),
            )
            context.last_error = str(exc)
            self.state_store.update(
                lambda state: record_backup_container(state, success=False),
            )
            return False

        selected_mounts = _log_mount_plan(group_name, container.name, mounts, dry_run=False)
        if not selected_mounts:
            LOGGER.warning(
                "container_has_no_backup_mounts",
                group=group_name,
                container=container.name,
            )
            self.state_store.update(
                lambda state: record_backup_container(state, success=True),
            )
            return True

        container_success = True
        backed_up = False
        for backup_mount in selected_mounts:
            if backup_mount.worker_mounts and not self.restic_runner.check_readable_path(
                backup_mount.mount.destination,
                backup_mount.worker_mounts,
            ):
                LOGGER.warning(
                    "mount_skipped_unreadable",
                    group=group_name,
                    container=container.name,
                    mount_type=backup_mount.mount.type,
                    mount_source=backup_mount.mount.source,
                    destination=backup_mount.mount.destination,
                )
                continue
            context.volumes_attempted += 1
            backup_started_at = utc_now()
            volume_started = time.perf_counter()
            result = self.restic_runner.backup_volume(
                container,
                backup_mount.mount.destination,
                member.labels.restic_args,
                worker_mounts=backup_mount.worker_mounts,
            )
            backup_completed_at = utc_now()
            volume_duration = time.perf_counter() - volume_started
            if result.exit_code != 0:
                context.last_error = _error_summary(result.output)
                LOGGER.error(
                    "backup_failed",
                    group=group_name,
                    container=container.name,
                    mount_type=backup_mount.mount.type,
                    mount_name=backup_mount.mount.name,
                    mount_source=backup_mount.mount.source,
                    destination=backup_mount.mount.destination,
                    exit_code=result.exit_code,
                    output=result.output,
                )
                self._record_backup_volume(
                    BackupRecord(
                        id=_backup_record_id(
                            group_name,
                            container.name,
                            backup_mount.display_name,
                            backup_completed_at,
                        ),
                        group=group_name,
                        container_name=container.name,
                        container_id=str(container.id),
                        volume_name=backup_mount.display_name,
                        volume_destination=backup_mount.mount.destination,
                        image_reference=container.image.reference,
                        image_id=container.image.image_id,
                        repo_digest=_first_repo_digest(container),
                        started_at=backup_started_at,
                        completed_at=backup_completed_at,
                        outcome="failure",
                        snapshot_paths=(backup_mount.mount.destination,),
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
                        backup_mount.display_name,
                        backup_completed_at,
                    ),
                    group=group_name,
                    container_name=container.name,
                    container_id=str(container.id),
                    volume_name=backup_mount.display_name,
                    volume_destination=backup_mount.mount.destination,
                    image_reference=container.image.reference,
                    image_id=container.image.image_id,
                    repo_digest=_first_repo_digest(container),
                    started_at=backup_started_at,
                    completed_at=backup_completed_at,
                    outcome="success",
                    snapshot_id=snapshot_id,
                    snapshot_paths=(backup_mount.mount.destination,),
                ),
                success=True,
                duration_seconds=volume_duration,
            )
            backed_up = True
            LOGGER.info(
                "backup_finished",
                group=group_name,
                container=container.name,
                mount_type=backup_mount.mount.type,
                mount_name=backup_mount.mount.name,
                mount_source=backup_mount.mount.source,
                destination=backup_mount.mount.destination,
            )
        if not backed_up and selected_mounts:
            LOGGER.warning(
                "container_had_no_readable_backup_mounts",
                group=group_name,
                container=container.name,
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
                    group=group_name,
                    container=container.name,
                )
            except Exception:
                LOGGER.exception(
                    "container_start_failed",
                    group=group_name,
                    container=container.name,
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


def _log_mount_plan(
    group_name: str,
    container_name: str,
    mounts: list[BackupMount],
    *,
    dry_run: bool,
) -> list[BackupMount]:
    selected: list[BackupMount] = []
    for backup_mount in mounts:
        event_prefix = "dry_run_" if dry_run else ""
        fields = {
            "group": group_name,
            "container": container_name,
            "mount_type": backup_mount.mount.type,
            "mount_name": backup_mount.mount.name,
            "mount_source": backup_mount.mount.source,
            "destination": backup_mount.mount.destination,
        }
        if backup_mount.skip_reason:
            LOGGER.warning(
                f"{event_prefix}mount_skipped",
                reason=backup_mount.skip_reason,
                **fields,
            )
            continue
        LOGGER.info(f"{event_prefix}mount_selected", **fields)
        selected.append(backup_mount)
    return selected
