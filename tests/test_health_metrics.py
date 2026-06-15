from __future__ import annotations

from datetime import timedelta

from backup.health import readiness
from backup.metrics import record_backup_run, record_backup_volume, render_prometheus
from backup.state import BackupState, utc_now


def test_readiness_is_unready_after_failed_backup(app_config):
    state = BackupState()
    state.runtime.last_run_success = False
    state.runtime.last_error = "backup failed"

    ready, reason = readiness(app_config, state)

    assert ready is False
    assert reason == "backup failed"


def test_readiness_fails_when_last_success_is_too_old(app_config):
    state = BackupState()
    state.runtime.last_run_success = True
    state.runtime.last_success_at = utc_now() - timedelta(days=3)

    ready, reason = readiness(app_config, state)

    assert ready is False
    assert "older than" in reason


def test_prometheus_metrics_include_status_counters_and_histograms(app_config):
    state = BackupState()
    state.runtime.last_run_success = True
    state.runtime.last_success_at = utc_now()
    record_backup_run(state, success=True, duration_seconds=12.5)
    record_backup_volume(state, success=False, duration_seconds=3.0)

    output = render_prometheus(state, app_config)

    assert 'opigen_backup_info{version="' in output
    assert 'image="worker:test"} 1' in output
    assert 'opigen_backup_runs_total{status="success"} 1' in output
    assert 'opigen_backup_volumes_total{status="failure"} 1' in output
    assert "opigen_backup_run_duration_seconds_count 1" in output
    assert 'opigen_backup_volume_duration_seconds_bucket{le="+Inf"} 1' in output
