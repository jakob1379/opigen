from __future__ import annotations

from dataclasses import dataclass

from backup.config import AppConfig
from backup.docker_client import DockerClient, DockerError, ImageMetadata
from backup.restic import ResticRunner
from backup.state import BackupRecord, StateStore


class RestoreError(RuntimeError):
    """Raised when a restore cannot be planned or executed safely."""


@dataclass(frozen=True)
class ImageDriftCheck:
    expected_reference: str
    expected_image_id: str | None
    expected_repo_digest: str | None
    current_image_id: str | None
    current_repo_digests: tuple[str, ...]
    drifted: bool
    reason: str | None = None


@dataclass(frozen=True)
class RestorePlan:
    backup: BackupRecord
    target_volume: str
    image_check: ImageDriftCheck

    @property
    def snapshot_path(self) -> str:
        return (
            self.backup.snapshot_paths[0]
            if self.backup.snapshot_paths
            else self.backup.volume_destination
        )


class RestoreManager:
    def __init__(
        self,
        *,
        config: AppConfig,
        docker_client: DockerClient,
        restic_runner: ResticRunner,
        state_store: StateStore,
    ) -> None:
        self.config = config
        self.docker_client = docker_client
        self.restic_runner = restic_runner
        self.state_store = state_store

    def list_backups(self) -> list[BackupRecord]:
        state = self.state_store.load()
        records = [
            record for record in state.backups if record.outcome == "success" and record.snapshot_id
        ]
        return sorted(records, key=lambda record: record.completed_at, reverse=True)

    def plan(self, backup_id: str, *, target_volume: str | None = None) -> RestorePlan:
        backup = self._find_backup(backup_id)
        if backup.snapshot_id is None:
            raise RestoreError(f"Backup {backup_id} does not have a restic snapshot id")
        target = target_volume or default_restore_volume_name(backup)
        return RestorePlan(
            backup=backup,
            target_volume=target,
            image_check=self._check_image_drift(backup),
        )

    def restore_volume(
        self,
        backup_id: str,
        *,
        target_volume: str | None = None,
        allow_image_drift: bool = False,
    ) -> RestorePlan:
        plan = self.plan(backup_id, target_volume=target_volume)
        if plan.image_check.drifted and not allow_image_drift:
            raise RestoreError(
                "Image identity has drifted; rerun with --allow-image-drift to restore anyway"
            )

        self.docker_client.create_volume(plan.target_volume)
        result = self.restic_runner.restore_volume(
            snapshot_id=plan.backup.snapshot_id or "",
            snapshot_path=plan.snapshot_path,
            target_volume=plan.target_volume,
        )
        if result.exit_code != 0:
            raise RestoreError(f"restic restore failed: {result.output}")
        return plan

    def _find_backup(self, backup_id: str) -> BackupRecord:
        for record in self.list_backups():
            if record.id == backup_id:
                return record
        raise RestoreError(f"Unknown restorable backup id: {backup_id}")

    def _check_image_drift(self, backup: BackupRecord) -> ImageDriftCheck:
        if not backup.image_reference:
            return ImageDriftCheck(
                expected_reference="",
                expected_image_id=backup.image_id,
                expected_repo_digest=backup.repo_digest,
                current_image_id=None,
                current_repo_digests=(),
                drifted=False,
                reason="backup did not record an image reference",
            )

        try:
            current = self.docker_client.image_metadata(backup.image_reference)
        except DockerError as exc:
            return _drifted_check(backup, reason=str(exc))
        except Exception as exc:
            return _drifted_check(backup, reason=f"failed to inspect image: {exc}")

        return compare_image_identity(backup, current)


def compare_image_identity(backup: BackupRecord, current: ImageMetadata) -> ImageDriftCheck:
    digest_matches = bool(backup.repo_digest and backup.repo_digest in current.repo_digests)
    image_id_matches = bool(
        backup.image_id and current.image_id and backup.image_id == current.image_id
    )
    has_expected_identity = bool(backup.repo_digest or backup.image_id)
    drifted = has_expected_identity and not (digest_matches or image_id_matches)
    reason = None
    if drifted:
        reason = "current image id/digest does not match the backup metadata"
    return ImageDriftCheck(
        expected_reference=backup.image_reference,
        expected_image_id=backup.image_id,
        expected_repo_digest=backup.repo_digest,
        current_image_id=current.image_id,
        current_repo_digests=current.repo_digests,
        drifted=drifted,
        reason=reason,
    )


def default_restore_volume_name(backup: BackupRecord) -> str:
    source = backup.volume_name or "volume"
    suffix = backup.completed_at.strftime("%Y%m%d%H%M%S")
    return f"opigen-restore-{source}-{suffix}"


def _drifted_check(backup: BackupRecord, *, reason: str) -> ImageDriftCheck:
    return ImageDriftCheck(
        expected_reference=backup.image_reference,
        expected_image_id=backup.image_id,
        expected_repo_digest=backup.repo_digest,
        current_image_id=None,
        current_repo_digests=(),
        drifted=True,
        reason=reason,
    )
