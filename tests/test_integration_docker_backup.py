from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import pytest

pytestmark = pytest.mark.integration


def test_backup_named_volume_with_docker_restic_worker(tmp_path: Path):
    if os.environ.get("OPIGEN_RUN_DOCKER_INTEGRATION") != "1":
        pytest.skip("set OPIGEN_RUN_DOCKER_INTEGRATION=1 to run Docker integration test")

    docker = pytest.importorskip("docker")
    client = docker.from_env()
    _require_docker(client)

    image = os.environ.get("OPIGEN_BACKUP_IMAGE", "opigen-backup:latest")
    fixture_image = os.environ.get(
        "OPIGEN_BACKUP_FIXTURE_IMAGE",
        "opigen-backup-test-fixture:latest",
    )
    _ensure_image(client, image, "dockerImage", tmp_path)
    _ensure_image(client, fixture_image, "testFixtureImage", tmp_path)

    run_id = f"opigen-itest-{uuid4().hex[:12]}"
    volume_name = f"{run_id}-data"
    repo_dir = tmp_path / "restic-repo"
    repo_dir.mkdir()
    state_file = tmp_path / "state.json"
    password_file = tmp_path / "restic-password"
    password_file.write_text("integration-secret\n", encoding="utf-8")
    config_file = tmp_path / "backup.toml"
    config_file.write_text(
        f"""
[backup]
repository = "/restic-repo"
password_file = "{password_file}"

[runtime]
worker_image = "{image}"
worker_mounts = ["{repo_dir}:/restic-repo:rw"]

[state]
path = "{state_file}"

[schedule]
enabled = false

[prune]
enabled = false

[check]
enabled = false

[timeouts]
stop_grace_period = 5
backup_operation = 120
""".strip(),
        encoding="utf-8",
    )

    volume = client.volumes.create(name=volume_name)
    container = None
    payload = f"opigen integration payload {run_id}\n"
    try:
        quoted_payload = shlex.quote(payload.rstrip("\n"))
        container = client.containers.run(
            image=fixture_image,
            command=[
                "sh",
                "-lc",
                "mkdir -p /data "
                f"&& printf '%s\n' {quoted_payload} > /data/payload.txt "
                "&& sleep 3600",
            ],
            detach=True,
            name=run_id,
            labels={
                "backup.enabled": "true",
                "backup.group": run_id,
                "backup.stop": "true",
                "backup.mounts": volume_name,
            },
            volumes={volume_name: {"bind": "/data", "mode": "rw"}},
        )
        _wait_for_payload(container)

        result = subprocess.run(
            [
                "nix",
                "run",
                ".#backup",
                "--",
                "run-once",
                "--config",
                str(config_file),
            ],
            cwd=Path(__file__).resolve().parents[1],
            text=True,
            capture_output=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr

        container.reload()
        assert container.status == "running"
        restore_volume_name = f"{run_id}-restored"
        restore_list = subprocess.run(
            [
                "nix",
                "run",
                ".#backup",
                "--",
                "restore",
                "list",
                "--config",
                str(config_file),
            ],
            cwd=Path(__file__).resolve().parents[1],
            text=True,
            capture_output=True,
        )
        assert restore_list.returncode == 0, restore_list.stdout + restore_list.stderr
        backup_id = json.loads(restore_list.stdout)[0]["id"]
        restore_result = subprocess.run(
            [
                "nix",
                "run",
                ".#backup",
                "--",
                "restore",
                "volume",
                backup_id,
                "--target-volume",
                restore_volume_name,
                "--config",
                str(config_file),
            ],
            cwd=Path(__file__).resolve().parents[1],
            text=True,
            capture_output=True,
        )
        assert restore_result.returncode == 0, restore_result.stdout + restore_result.stderr
        restored_from_volume = client.containers.run(
            image=fixture_image,
            command=["cat", "/data/payload.txt"],
            volumes={restore_volume_name: {"bind": "/data", "mode": "ro"}},
            remove=True,
        )
        if isinstance(restored_from_volume, bytes):
            restored_from_volume = restored_from_volume.decode("utf-8")
        assert restored_from_volume == payload

        restored_payload = client.containers.run(
            image=image,
            command=["restic", "dump", "latest", "/data/payload.txt"],
            environment={
                "RESTIC_REPOSITORY": "/restic-repo",
                "RESTIC_PASSWORD": "integration-secret",
            },
            user="0:0",
            volumes={str(repo_dir): {"bind": "/restic-repo", "mode": "rw"}},
            remove=True,
        )

        if isinstance(restored_payload, bytes):
            restored_payload = restored_payload.decode("utf-8")
        assert restored_payload == payload
    finally:
        if container is not None:
            container.remove(force=True)
        try:
            client.volumes.get(f"{run_id}-restored").remove(force=True)
        except Exception:
            pass
        volume.remove(force=True)


def _require_docker(client) -> None:
    try:
        client.ping()
    except Exception as exc:
        pytest.skip(f"Docker daemon is unavailable: {exc}")


def _ensure_image(client, image: str, output: str, tmp_path: Path) -> None:
    force_load = os.environ.get("OPIGEN_RELOAD_DOCKER_IMAGES") == "1"
    if not force_load:
        try:
            client.images.get(image)
            return
        except Exception:
            pass

    out_link = tmp_path / output
    subprocess.run(
        ["nix", "build", f".#{output}", "--out-link", str(out_link)],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        text=True,
        capture_output=True,
    )
    client.images.load(out_link.read_bytes())
    client.images.get(image)


def _wait_for_payload(container) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        result = container.exec_run(["sh", "-lc", "test -f /data/payload.txt"])
        if result.exit_code == 0:
            return
        time.sleep(0.25)
    raise AssertionError("test container did not create /data/payload.txt")
