from __future__ import annotations

from pathlib import Path

import pytest

from backup.config import parse_config


@pytest.fixture
def secret_files(tmp_path: Path) -> dict[str, Path]:
    password = tmp_path / "password"
    access = tmp_path / "access"
    secret = tmp_path / "secret"
    password.write_text("restic-pass\n", encoding="utf-8")
    access.write_text("access-key\n", encoding="utf-8")
    secret.write_text("secret-key\n", encoding="utf-8")
    return {"password": password, "access": access, "secret": secret}


@pytest.fixture
def app_config(secret_files: dict[str, Path], tmp_path: Path):
    return parse_config(
        {
            "backup": {
                "repository": "s3:http://example/backups",
                "password_file": str(secret_files["password"]),
                "aws_access_key_id_file": str(secret_files["access"]),
                "aws_secret_access_key_file": str(secret_files["secret"]),
            },
            "runtime": {"worker_image": "worker:test"},
            "state": {"path": str(tmp_path / "state.json")},
            "restic": {"global_args": "--verbose"},
        }
    )
