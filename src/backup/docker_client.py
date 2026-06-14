from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backup.config import WorkerMount


@dataclass(frozen=True)
class Mount:
    type: str
    name: str | None
    destination: str


class DockerError(RuntimeError):
    """Raised for Docker orchestration failures."""


class DockerContainer:
    def __init__(self, raw: Any):
        self.raw = raw
        self._load()

    def _load(self) -> None:
        attrs = self.raw.attrs
        self.id = self.raw.id
        self.name = self.raw.name
        self.status = getattr(self.raw, "status", attrs.get("State", {}).get("Status", "unknown"))
        self.labels = dict(attrs.get("Config", {}).get("Labels") or {})
        self.mounts = [
            Mount(
                type=str(mount.get("Type", "")),
                name=mount.get("Name"),
                destination=str(mount.get("Destination", "")),
            )
            for mount in attrs.get("Mounts", [])
        ]

    @property
    def running(self) -> bool:
        return self.status == "running"

    def reload(self) -> None:
        self.raw.reload()
        self._load()

    def stop(self, timeout: int) -> None:
        self.raw.stop(timeout=timeout)
        self.reload()

    def start(self) -> None:
        self.raw.start()
        self.reload()


@dataclass(frozen=True)
class WorkerResult:
    exit_code: int
    output: str


class DockerClient:
    def __init__(self) -> None:
        try:
            import docker
        except ImportError as exc:  # pragma: no cover - exercised only in uninstalled runtimes
            raise DockerError("docker Python SDK is required") from exc

        self.client = docker.from_env()

    def list_backup_containers(self) -> list[DockerContainer]:
        containers = self.client.containers.list(
            all=True,
            filters={"label": "backup.enabled=true"},
        )
        return [DockerContainer(container) for container in containers]

    def run_worker(
        self,
        *,
        image: str,
        command: list[str],
        environment: dict[str, str],
        timeout: int,
        source_container: DockerContainer | None = None,
        worker_mounts: tuple[WorkerMount, ...] = (),
    ) -> WorkerResult:
        kwargs: dict[str, Any] = {
            "tmpfs": {"/tmp": "rw,nosuid,nodev,size=64m"},  # nosec B108: container tmpfs mount.
        }
        if source_container is not None:
            kwargs["volumes_from"] = [source_container.id]
        if worker_mounts:
            kwargs["volumes"] = {
                mount.source: {"bind": mount.target, "mode": mount.mode} for mount in worker_mounts
            }
        worker = self.client.containers.run(
            image=image,
            command=command,
            environment=environment,
            detach=True,
            remove=False,
            **kwargs,
        )
        try:
            wait_result = worker.wait(timeout=timeout)
            status_code = int(wait_result.get("StatusCode", 1))
            logs = worker.logs(stdout=True, stderr=True)
            output = (
                logs.decode("utf-8", errors="replace") if isinstance(logs, bytes) else str(logs)
            )
            return WorkerResult(exit_code=status_code, output=output)
        finally:
            try:
                worker.remove(force=True)
            except Exception as exc:
                raise DockerError(f"Failed to remove worker container: {exc}") from exc
