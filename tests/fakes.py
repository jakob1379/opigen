from __future__ import annotations

from dataclasses import dataclass, field

from backup.docker_client import Mount, WorkerResult


@dataclass
class FakeContainer:
    name: str
    status: str = "running"
    labels: dict[str, str] = field(default_factory=dict)
    mounts: list[Mount] = field(default_factory=list)
    id: str | None = None
    stop_error: Exception | None = None
    start_error: Exception | None = None
    events: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.id is None:
            self.id = self.name

    @property
    def running(self) -> bool:
        return self.status == "running"

    def stop(self, timeout: int) -> None:
        self.events.append(f"stop:{timeout}")
        if self.stop_error is not None:
            raise self.stop_error
        self.status = "exited"

    def start(self) -> None:
        self.events.append("start")
        if self.start_error is not None:
            raise self.start_error
        self.status = "running"


class FakeDockerClient:
    def __init__(self, containers: list[FakeContainer] | None = None):
        self.containers = containers or []
        self.worker_calls = []
        self.results: list[WorkerResult] = []

    def list_backup_containers(self):
        return self.containers

    def run_worker(self, **kwargs):
        self.worker_calls.append(kwargs)
        if self.results:
            return self.results.pop(0)
        return WorkerResult(exit_code=0, output="")
