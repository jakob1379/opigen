from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass

from backup.config import AppConfig
from backup.docker_client import DockerClient, DockerContainer
from backup.labels import ContainerBackupLabels, LabelError, parse_container_labels
from backup.restic import ResticRunner
from backup.state import StateStore
from backup.volumes import VolumeSelectionError, select_named_volumes

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class BackupMember:
    container: DockerContainer
    labels: ContainerBackupLabels


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
        groups = self.discover_groups()
        success = True
        for group_name in sorted(groups):
            group_success = self._process_group(group_name, groups[group_name])
            success = success and group_success

        try:
            self.restic_runner.run_maintenance(self.state_store)
        except Exception:
            LOGGER.exception("maintenance_failed")
            success = False
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

    def _process_group(self, group_name: str, members: list[BackupMember]) -> bool:
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
                    self._restart_stopped(group_name, stopped_by_run)
                    return False

        try:
            for member in members:
                if not self._backup_container(group_name, member):
                    group_success = False
        finally:
            if not self._restart_stopped(group_name, stopped_by_run):
                group_success = False

        LOGGER.info("group_finished", extra={"group": group_name, "success": group_success})
        return group_success

    def _backup_container(self, group_name: str, member: BackupMember) -> bool:
        container = member.container
        try:
            volumes = select_named_volumes(container.mounts, member.labels.volumes)
        except VolumeSelectionError as exc:
            LOGGER.error(
                "volume_selection_failed",
                extra={"group": group_name, "container": container.name, "error": str(exc)},
            )
            return False

        if not volumes:
            LOGGER.warning(
                "container_has_no_named_volumes",
                extra={"group": group_name, "container": container.name},
            )
            return True

        container_success = True
        for volume in volumes:
            result = self.restic_runner.backup_volume(
                container,
                volume.destination,
                member.labels.restic_args,
            )
            if result.exit_code != 0:
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
                container_success = False
                continue
            LOGGER.info(
                "backup_finished",
                extra={
                    "group": group_name,
                    "container": container.name,
                    "volume": volume.name,
                    "destination": volume.destination,
                },
            )
        return container_success

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
