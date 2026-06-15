from __future__ import annotations

import json
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import structlog

from backup.config import AppConfig, load_config
from backup.metrics import render_prometheus
from backup.state import BackupState, StateStore, utc_now

LOGGER = structlog.get_logger(__name__)


class HealthServer:
    def __init__(self, *, config_path: Path, config: AppConfig):
        self.config_path = config_path
        self.config = config
        self.server = ThreadingHTTPServer(
            (config.health.bind_host, config.health.port),
            self._handler_class(),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> None:
        LOGGER.info(
            "health_server_started",
            bind_host=self.config.health.bind_host,
            port=self.config.health.port,
        )
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    def _handler_class(self):
        config_path = self.config_path

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                try:
                    config = load_config(config_path)
                    state = StateStore(config.state.path).load()
                    if self.path == "/healthz":
                        self._json_response(200, {"status": "ok"})
                        return
                    if self.path == "/readyz":
                        ready, reason = readiness(config, state)
                        self._json_response(
                            200 if ready else 503,
                            {"status": "ok" if ready else "unready", "reason": reason},
                        )
                        return
                    if self.path == "/metrics":
                        body = render_prometheus(state, config).encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "text/plain; version=0.0.4")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    self._json_response(404, {"status": "not_found"})
                except Exception as exc:
                    LOGGER.exception("health_request_failed")
                    self._json_response(503, {"status": "error", "error": str(exc)})

            def log_message(self, format: str, *args) -> None:
                LOGGER.debug("health_http_log", message=format % args)

            def _json_response(self, status: int, payload: dict[str, object]) -> None:
                body = json.dumps(payload, sort_keys=True).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler


def readiness(config: AppConfig, state: BackupState) -> tuple[bool, str]:
    runtime = state.runtime
    if runtime.last_run_success is False:
        return False, runtime.last_error or "last backup run failed"

    last_success = runtime.last_success_at
    if last_success is None:
        if runtime.last_run_completed_at is None:
            return True, "no backup has completed yet"
        return False, "no successful backup has completed"

    max_age = readiness_max_age(config)
    age = utc_now() - last_success
    if age > max_age:
        return False, f"last successful backup is older than {int(max_age.total_seconds())}s"
    return True, "ready"


def readiness_max_age(config: AppConfig) -> timedelta:
    if config.health.readiness_max_age_seconds is not None:
        return timedelta(seconds=config.health.readiness_max_age_seconds)
    intervals = {
        "hourly": timedelta(hours=1),
        "daily": timedelta(days=1),
        "weekly": timedelta(weeks=1),
    }
    return intervals[config.schedule.frequency] * 2
