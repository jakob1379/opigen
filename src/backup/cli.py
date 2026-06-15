from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from backup.config import load_config
from backup.docker_client import DockerClient
from backup.health import HealthServer
from backup.json_logging import configure_logging
from backup.orchestrator import BackupOrchestrator
from backup.restic import ResticRunner
from backup.restore import RestoreError, RestoreManager
from backup.scheduler import serve as serve_scheduler
from backup.state import BackupRecord, StateStore

app = typer.Typer(
    context_settings={"help_option_names": ["-h", "--help"]},
    no_args_is_help=False,
)
restore_app = typer.Typer(help="Inspect and restore backed-up Docker volumes.")
app.add_typer(restore_app, name="restore")

ConfigOption = Annotated[
    Path,
    typer.Option(
        "--config",
        envvar="CONFIG_PATH",
        help="Path to the backup TOML config.",
    ),
]
LogLevelOption = Annotated[
    str,
    typer.Option(
        "--log-level",
        envvar="LOG_LEVEL",
        help="Python logging level.",
    ),
]


@app.callback(invoke_without_command=True)
def default(ctx: typer.Context) -> None:
    """Run scheduled backups when no subcommand is provided."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(serve)


@app.command("run-once")
def run_once(
    config: ConfigOption = Path("/config/backup.toml"),
    log_level: LogLevelOption = "INFO",
) -> None:
    """Run one backup cycle and exit."""
    orchestrator = _build_orchestrator(config, log_level)
    if not orchestrator.run_once():
        raise typer.Exit(code=1)


@app.command()
def serve(
    config: ConfigOption = Path("/config/backup.toml"),
    log_level: LogLevelOption = "INFO",
) -> None:
    """Run the scheduled backup service."""
    config_data = load_config(config)
    orchestrator = _build_orchestrator(config, log_level)
    state_store = orchestrator.state_store
    health_server = HealthServer(config_path=config, config=config_data)
    health_server.start()
    try:
        serve_scheduler(
            config_data.schedule,
            orchestrator.run_once,
            on_next_run=lambda next_run: state_store.update(
                lambda state: setattr(state.runtime, "next_run_at", next_run),
            ),
        )
    finally:
        health_server.stop()


@restore_app.command("list")
def restore_list(
    config: ConfigOption = Path("/config/backup.toml"),
    log_level: LogLevelOption = "INFO",
) -> None:
    """List restorable backup records."""
    manager = _build_restore_manager(config, log_level)
    typer.echo(json.dumps([_backup_payload(record) for record in manager.list_backups()], indent=2))


@restore_app.command("plan")
def restore_plan(
    backup_id: Annotated[str, typer.Argument(help="Backup record id from restore list.")],
    config: ConfigOption = Path("/config/backup.toml"),
    log_level: LogLevelOption = "INFO",
    target_volume: Annotated[
        str | None,
        typer.Option("--target-volume", help="Docker volume to restore into."),
    ] = None,
) -> None:
    """Show the restore target and image identity check for a backup."""
    manager = _build_restore_manager(config, log_level)
    try:
        plan = manager.plan(backup_id, target_volume=target_volume)
    except RestoreError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps(_restore_plan_payload(plan), indent=2))


@restore_app.command("volume")
def restore_volume(
    backup_id: Annotated[str, typer.Argument(help="Backup record id from restore list.")],
    config: ConfigOption = Path("/config/backup.toml"),
    log_level: LogLevelOption = "INFO",
    target_volume: Annotated[
        str | None,
        typer.Option("--target-volume", help="Docker volume to restore into."),
    ] = None,
    allow_image_drift: Annotated[
        bool,
        typer.Option(
            "--allow-image-drift",
            help="Restore even when the current image identity differs from backup metadata.",
        ),
    ] = False,
) -> None:
    """Restore one backed-up named Docker volume into a new or selected volume."""
    manager = _build_restore_manager(config, log_level)
    try:
        plan = manager.restore_volume(
            backup_id,
            target_volume=target_volume,
            allow_image_drift=allow_image_drift,
        )
    except RestoreError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps(_restore_plan_payload(plan), indent=2))


def main(argv: list[str] | None = None) -> int:
    try:
        app(args=argv, standalone_mode=False)
    except typer.Exit as exc:
        return int(exc.exit_code or 0)
    return 0


def _build_orchestrator(config: Path, log_level: str) -> BackupOrchestrator:
    configure_logging(log_level)
    config_data = load_config(config)
    docker_client = DockerClient()
    restic_runner = ResticRunner(config_data, docker_client)
    return BackupOrchestrator(
        config=config_data,
        docker_client=docker_client,
        restic_runner=restic_runner,
        state_store=StateStore(config_data.state.path),
    )


def _build_restore_manager(config: Path, log_level: str) -> RestoreManager:
    configure_logging(log_level)
    config_data = load_config(config)
    docker_client = DockerClient()
    restic_runner = ResticRunner(config_data, docker_client)
    return RestoreManager(
        config=config_data,
        docker_client=docker_client,
        restic_runner=restic_runner,
        state_store=StateStore(config_data.state.path),
    )


def _backup_payload(record: BackupRecord) -> dict[str, object]:
    return {
        "id": record.id,
        "group": record.group,
        "container_name": record.container_name,
        "container_id": record.container_id,
        "volume_name": record.volume_name,
        "volume_destination": record.volume_destination,
        "image_reference": record.image_reference,
        "image_id": record.image_id,
        "repo_digest": record.repo_digest,
        "completed_at": record.completed_at.isoformat(),
        "snapshot_id": record.snapshot_id,
        "snapshot_paths": list(record.snapshot_paths),
    }


def _restore_plan_payload(plan: Any) -> dict[str, object]:
    check = plan.image_check
    return {
        "backup": _backup_payload(plan.backup),
        "target_volume": plan.target_volume,
        "snapshot_path": plan.snapshot_path,
        "image_check": {
            "expected_reference": check.expected_reference,
            "expected_image_id": check.expected_image_id,
            "expected_repo_digest": check.expected_repo_digest,
            "current_image_id": check.current_image_id,
            "current_repo_digests": list(check.current_repo_digests),
            "drifted": check.drifted,
            "reason": check.reason,
        },
    }
