"""Cancellation must stop the process tree and scope cleanup to one run."""

import json
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from web import live_cycle


@pytest.fixture
def runner(tmp_path, monkeypatch):
    monkeypatch.setattr(
        live_cycle, "BASE_CONFIG", live_cycle.REPO / "config" / "experiment.draft.yaml"
    )
    monkeypatch.setattr(live_cycle, "OUT_ROOT", tmp_path)
    return live_cycle.LiveCycleRunner()


def test_idle_stop_and_reset_keep_saved_history(runner, tmp_path):
    receipt = tmp_path / "saved-receipt.json"
    receipt.write_text('{"outcome":"executed"}')
    assert runner.stop()[0] is False
    assert runner.reset()[0] is True
    assert runner.state()["current"]["phase"] == "idle"
    assert runner.state()["current"]["reset_at"]
    assert json.loads(receipt.read_text())["outcome"] == "executed"


def test_cleanup_only_deletes_jobs_labelled_for_the_current_run(runner, monkeypatch, tmp_path):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(live_cycle.subprocess, "run", run)
    runner._state = {"phase": "stopping", "run_id": "web-test", "events": []}
    runner._finish_cancel("web-test")
    assert len(commands) == 2
    for command in commands:
        assert command[command.index("-l") + 1] == (
            "app.kubernetes.io/managed-by=carbon-scheduler,carbon-scheduler/run-id=web-test")
        assert command[command.index("-n") + 1] == "benchmark"
        assert "--cascade=foreground" in command
        assert "--wait=true" in command
        assert "deployments" not in command
    record = json.loads((tmp_path / "web-test" / "cancellation.json").read_text())
    assert record["phase"] == "cancelled"
    assert "receipt" not in record


def test_cleanup_failure_blocks_reset_and_new_run_then_can_retry(runner, monkeypatch):
    monkeypatch.setattr(live_cycle.subprocess, "run", lambda *a, **kw:
        SimpleNamespace(returncode=1, stderr="cluster unreachable"))
    runner._state = {"phase": "stopping", "run_id": "web-test", "events": []}
    runner._finish_cancel("web-test")
    assert runner.state()["current"]["phase"] == "cancel_failed"
    assert not runner.reset()[0]
    assert not runner.start({"east": "ready", "west": "ready"})[0]
    monkeypatch.setattr(live_cycle.subprocess, "run", lambda *a, **kw:
        SimpleNamespace(returncode=0, stderr=""))
    assert runner.stop()[0]
    runner._thread.join(timeout=3)
    assert runner.state()["current"]["phase"] == "cancelled"
    assert runner.reset()[0]


def test_cancelled_runner_cannot_launch_another_process(runner, tmp_path):
    runner._cancel.set()
    with (tmp_path / "log").open("w") as log, pytest.raises(live_cycle.CycleCancelled):
        runner._launch([sys.executable, "-c", "raise RuntimeError('must not launch')"], log)


def test_stop_terminates_local_process_tree_during_capture(runner, tmp_path):
    child_pid_file = tmp_path / "child.pid"
    script = (
        "import subprocess,sys,time; from pathlib import Path; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(40)']); "
        f"Path({str(child_pid_file)!r}).write_text(str(child.pid)); time.sleep(40)"
    )
    cancelled = threading.Event()

    def capture():
        try:
            runner._call([sys.executable, "-c", script], tmp_path / "capture.log", timeout=45)
        except live_cycle.CycleCancelled:
            cancelled.set()

    runner._state = {"phase": "capturing", "run_id": "web-test", "events": []}
    runner._thread = threading.Thread(target=capture)
    runner._thread.start()
    try:
        deadline = time.monotonic() + 5
        while not child_pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert child_pid_file.exists()
        assert runner.stop()[0]
        runner._thread.join(timeout=5)
        assert not runner._thread.is_alive()
        assert cancelled.is_set()
        assert runner._process is None
        child_pid = int(child_pid_file.read_text())
        stat = Path(f"/proc/{child_pid}/stat")
        deadline = time.monotonic() + 2
        while stat.exists() and stat.read_text().split()[2] != "Z" and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not stat.exists() or stat.read_text().split()[2] == "Z"
    finally:
        runner._cancel.set()
        runner._terminate_process()
        runner._thread.join(timeout=5)


def test_cancel_during_capture_records_cancellation_without_a_receipt(runner, monkeypatch, tmp_path):
    def capture(*args, **kwargs):
        runner._cancel.set()
        raise live_cycle.CycleCancelled()

    monkeypatch.setattr(runner, "_call", capture)
    monkeypatch.setattr(live_cycle.subprocess, "run", lambda *a, **kw:
        SimpleNamespace(returncode=0, stderr=""))
    runner._state = {"phase": "capturing", "run_id": "web-test", "events": []}
    runner._run("web-test", None)
    assert runner.state()["current"]["phase"] == "cancelled"
    assert (tmp_path / "web-test" / "cancellation.json").exists()
    assert not (tmp_path / "web-test" / "runs").exists()


def test_stop_arriving_during_failure_reporting_still_cleans_up(runner, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("capture failed")

    original_event = runner._event

    def event(phase, text, **extra):
        if phase == "failed":
            runner._cancel.set()
        return original_event(phase, text, **extra)

    monkeypatch.setattr(runner, "_call", fail)
    monkeypatch.setattr(runner, "_event", event)
    monkeypatch.setattr(live_cycle.subprocess, "run", lambda *a, **kw:
        SimpleNamespace(returncode=0, stderr=""))
    runner._state = {"phase": "capturing", "run_id": "web-test", "events": []}
    runner._run("web-test", None)
    assert runner.state()["current"]["phase"] == "cancelled"


@pytest.mark.parametrize("request_time", ["2026-10-28T10:05:00+00:00", "2026-10-28T10:09:59+00:00"])
def test_capture_gets_full_decision_budget_near_slot_boundary(runner, monkeypatch, tmp_path, request_time):
    now = datetime.fromisoformat(request_time)
    monkeypatch.setattr(live_cycle, "_now", lambda: now)
    observed = {}

    def capture(command, *args, **kwargs):
        config = live_cycle.yaml.safe_load(Path(command[command.index("--config") + 1]).read_text())
        observed.update(config["workload"])
        return 1  # End the test at capture; never call a model or Kubernetes.

    monkeypatch.setattr(runner, "_call", capture)
    runner._state = {"phase": "starting", "run_id": "web-test", "events": []}
    runner._run("web-test", None)
    deadline = datetime.fromisoformat(observed["deadline"].replace("Z", "+00:00"))
    earliest = datetime.fromisoformat(observed["earliest_start"].replace("Z", "+00:00"))
    assert deadline - now == timedelta(minutes=5)
    assert earliest == datetime(2026, 10, 28, 10, 5, tzinfo=UTC)
