from __future__ import annotations

from backup.config import WorkerMount
from backup.docker_client import DockerClient, DockerContainer


class FakeWorker:
    def wait(self, timeout: int):
        assert timeout == 120
        return {"StatusCode": 0}

    def logs(self, stdout: bool, stderr: bool):
        assert stdout is True
        assert stderr is True
        return b"ok"

    def remove(self, force: bool) -> None:
        assert force is True


class FakeContainers:
    def __init__(self) -> None:
        self.run_kwargs = None

    def run(self, **kwargs):
        self.run_kwargs = kwargs
        return FakeWorker()


class FakeClient:
    def __init__(self) -> None:
        self.containers = FakeContainers()


class FakeImage:
    id = "sha256:resolved"
    attrs = {"RepoDigests": ["postgres@sha256:abc"]}


class FakeRawContainer:
    id = "container-id"
    name = "db"
    status = "running"
    image = FakeImage()
    attrs = {
        "Config": {
            "Image": "postgres:16",
            "Labels": {"backup.enabled": "true"},
        },
        "Image": "sha256:original",
        "Mounts": [{"Type": "volume", "Name": "pgdata", "Destination": "/var/lib/postgresql/data"}],
    }


def test_run_worker_uses_hardened_container_options():
    docker_client = object.__new__(DockerClient)
    docker_client.client = FakeClient()

    result = docker_client.run_worker(
        image="worker:test",
        command=["restic", "snapshots"],
        environment={"RESTIC_REPOSITORY": "/repo"},
        timeout=120,
        worker_mounts=(WorkerMount(source="/host/repo", target="/repo", mode="rw"),),
    )

    assert result.exit_code == 0
    assert result.output == "ok"
    run_kwargs = docker_client.client.containers.run_kwargs
    assert run_kwargs["read_only"] is True
    assert run_kwargs["security_opt"] == ["no-new-privileges:true"]
    assert run_kwargs["tmpfs"] == {"/tmp": "rw,nosuid,nodev,size=64m"}
    assert run_kwargs["user"] == "0:0"
    assert run_kwargs["volumes"] == {"/host/repo": {"bind": "/repo", "mode": "rw"}}


def test_container_captures_image_metadata():
    container = DockerContainer(FakeRawContainer())

    assert container.image.reference == "postgres:16"
    assert container.image.image_id == "sha256:original"
    assert container.image.repo_digests == ("postgres@sha256:abc",)
