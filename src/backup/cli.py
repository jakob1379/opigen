from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from backup.config import load_config
from backup.docker_client import DockerClient
from backup.json_logging import configure_logging
from backup.orchestrator import BackupOrchestrator
from backup.restic import ResticRunner
from backup.scheduler import serve as serve_scheduler
from backup.state import StateStore

app = typer.Typer(
    context_settings={"help_option_names": ["-h", "--help"]},
    no_args_is_help=False,
)

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
    serve_scheduler(config_data.schedule, orchestrator.run_once)


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
