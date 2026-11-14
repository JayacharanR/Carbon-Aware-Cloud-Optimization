"""Run one real scheduling cycle from the dashboard.

This is the production path the 12 Oct 2026 live session used, with nothing
simulated:

1. capture a fresh Electricity Maps snapshot (scripts/capture_carbon.py, zone-keyed);
2. run ``carbon-scheduler --mode hybrid --gate-profile v2.1 --max-cycles 1``
   without ``--dry-run``: Llama-3 proposes, gate v2.1 checks, and the executor
   creates the fixture-reset and wrk2 benchmark Jobs on the chosen AKS cluster;
3. read the cycle receipt (cycles.jsonl) when the CLI exits.

While step 2 runs, both clusters are polled for this cycle's Jobs, so the page
can show which cluster the work really landed on before the receipt exists.
Artifacts go to artifacts/web-cycles/<run_id>/.

Scenario "spike": after the real capture, the currently cleaner region's
intensity (current value and whole forecast) is raised to at least 3x, and to
more than the other region, in a copy of the trace. The scheduler decides on
that copy and its decision is executed for real, so the page can show an
actual switch of cluster. The unmodified capture and the change are kept
(carbon-trace.json, scenario.json) and the receipt is labelled.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parent.parent
BASE_CONFIG = REPO / "config" / "experiment.yaml"
TARGETS = REPO / "config" / "targets.yaml"
GRAPH = REPO / "artifacts" / "graph.json"
OUT_ROOT = REPO / "artifacts" / "web-cycles"

CLUSTERS = {"eastus2": "aks-primary-eastus2", "westus2": "aks-secondary-westus2"}
NAMESPACE = "benchmark"
SLOT_MINUTES = 5
# Keep the current slot available, but give capture and inference a full five
# minutes from the request, even when it arrives just before a slot boundary.
WINDOW_MINUTES = 5
CLI_TIMEOUT_SECONDS = 30 * 60
SCENARIOS = {"spike"}
SPIKE_FACTOR = 3.0


class CycleCancelled(Exception):
    """The operator stopped this cycle."""


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _floor_to_slot(moment: datetime) -> datetime:
    moment = moment.replace(second=0, microsecond=0)
    return moment - timedelta(minutes=moment.minute % SLOT_MINUTES)


def summarize_run(run_dir: Path) -> dict[str, Any] | None:
    """Receipt fields the page shows, read from a finished run's cycles.jsonl."""

    cycles = run_dir / "cycles.jsonl"
    if not cycles.exists() or not cycles.read_text(encoding="utf-8").strip():
        return None
    record = json.loads(cycles.read_text(encoding="utf-8").strip().splitlines()[-1])
    receipt = record.get("execution_receipt") or {}
    trust = record.get("trust_report") or {}
    agent = record.get("agent_proposal") or {}
    final = receipt.get("final_action") or record.get("proposal") or {}
    regions = (record.get("decision_input") or {}).get("carbon_snapshot", {}).get("regions", {})
    carbon = {
        region: {
            "intensity": (info.get("current") or {}).get("carbon_intensity_gco2eq_per_kwh"),
            "data_hour": (info.get("current") or {}).get("timestamp"),
            "zone": info.get("reported_zone"),
        }
        for region, info in regions.items()
    }
    failed_checks = [c.get("name") for c in trust.get("rail_checks") or [] if not c.get("passed")]
    scenario_file = run_dir.parent.parent / "scenario.json"
    scenario = json.loads(scenario_file.read_text(encoding="utf-8")) if scenario_file.exists() else None
    log_excerpt: list[str] = []
    for path in sorted((run_dir / "benchmark-logs").glob("*-benchmark.log")):
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        keep = [l for l in lines if l.strip() and not l.lstrip().startswith(("0.", "1.")) and "Value   Percentile" not in l]
        log_excerpt = [l for l in keep if any(k in l for k in (
            "Running", "threads and", "Latency", "Req/Sec", "requests in", "Requests/sec", "Transfer/sec",
            "Socket errors", "Non-2xx", "50.000%", "90.000%", "99.000%", "Thread Stats"))][:16]
    return {
        "run_id": record.get("decision_input", {}).get("run_id") or run_dir.name,
        "outcome": record.get("outcome"),
        "decision_source": record.get("decision_source"),
        "gate_profile": record.get("gate_profile"),
        "failure_reason": record.get("failure_reason"),
        "llm_proposed_region": agent.get("target_region"),
        "llm_scheduled_time": agent.get("scheduled_time"),
        "final_region": final.get("target_region"),
        "scheduled_time": final.get("scheduled_time"),
        "gate_passed": trust.get("passed"),
        "gate_failed_checks": failed_checks,
        "gate_failure_reason": trust.get("failure_reason"),
        "carbon_regret": trust.get("carbon_regret"),
        "gate_checks_total": len(trust.get("rail_checks") or []),
        "evidence_score": trust.get("weighted_score"),
        "faithfulness": (trust.get("components") or {}).get("ragas_faithfulness"),
        "context_precision": (trust.get("components") or {}).get("ragas_context_precision"),
        "carbon": carbon,
        "cluster": receipt.get("cluster_name"),
        "job_name": receipt.get("job_name"),
        "dispatched_at": receipt.get("dispatched_at"),
        "completed_at": receipt.get("completed_at"),
        "p95_ms": receipt.get("actual_p95_latency_ms"),
        "error_rate": receipt.get("actual_error_rate"),
        "slo_passed": receipt.get("slo_passed"),
        "execution_failure_reason": receipt.get("execution_failure_reason"),
        "log_excerpt": log_excerpt,
        "scenario": scenario,
    }


class LiveCycleRunner:
    """At most one real cycle at a time; state is kept in memory and on disk."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {"phase": "idle"}
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._process: subprocess.Popen | None = None

    # ------------------------------------------------------------------ API
    def state(self) -> dict[str, Any]:
        with self._lock:
            current = dict(self._state)
            current["worker_active"] = self._thread is not None and self._thread.is_alive()
        return {"current": current, "history": self.history()}

    def history(self, limit: int = 10) -> list[dict[str, Any]]:
        if not OUT_ROOT.exists():
            return []
        runs = []
        for run_dir in sorted(OUT_ROOT.glob("web-*/runs/*"), reverse=True):
            summary = summarize_run(run_dir)
            if summary:
                runs.append(summary)
            if len(runs) >= limit:
                break
        return runs

    def start(self, cluster_states: dict[str, str], scenario: str | None = None) -> tuple[bool, str]:
        if scenario is not None and scenario not in SCENARIOS:
            return False, f"Unknown scenario {scenario!r}"
        not_ready = [f"{name} is {state}" for name, state in cluster_states.items() if state != "ready"]
        if not_ready:
            return False, "Both clusters must be ready: " + ", ".join(not_ready)
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False, "A live cycle is already running"
            if self._state["phase"] == "cancel_failed":
                return False, "Retry force stop to finish cleaning up the previous cycle"
            self._cancel.clear()
            run_id = "web-" + _now().strftime("%Y%m%d-%H%M%S-%f") + (f"-{scenario}" if scenario else "")
            previous = next((r for r in self.history(limit=1) if r.get("cluster")), None)
            self._state = {"phase": "starting", "run_id": run_id, "started_at": _now().isoformat(),
                           "events": [], "cluster": None, "region": None, "jobs": {}, "scenario": None,
                           "previous_cluster": previous["cluster"] if previous else None}
            self._thread = threading.Thread(target=self._run, args=(run_id, scenario), name="LiveCycle", daemon=True)
            self._thread.start()
        return True, run_id

    def stop(self) -> tuple[bool, str]:
        """Cancel only the current cycle, including its child processes and Jobs."""
        with self._lock:
            if self._state["phase"] == "cancel_failed":
                if self._thread is not None and self._thread.is_alive():
                    return True, "Cleanup is finishing"
                run_id = self._state["run_id"]
                self._state["phase"] = "stopping"
                self._thread = threading.Thread(target=self._finish_cancel, args=(run_id,), daemon=True)
                self._thread.start()
                return True, "Retrying run-specific Job cleanup"
            if self._thread is None or not self._thread.is_alive() or self._state["phase"] in {"idle", "completed", "failed", "cancelled"}:
                return False, "No live cycle is running"
            self._cancel.set()
            self._state["phase"] = "stopping"
            self._state.setdefault("events", []).append({"at": _now().isoformat(), "phase": "stopping",
                "text": "Stop requested. Terminating this cycle and removing its reset and benchmark Jobs."})
            proc = self._process
        if proc is not None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        return True, "Stopping the live cycle"

    def reset(self) -> tuple[bool, str]:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False, "Wait for the live cycle to stop before resetting"
            if self._state["phase"] == "cancel_failed":
                return False, "Job cleanup failed. Retry force stop before resetting"
            self._state = {"phase": "idle", "reset_at": _now().isoformat()}
        return True, "Presentation reset. Saved run history is preserved."

    def _checkpoint(self) -> None:
        if self._cancel.is_set():
            raise CycleCancelled()

    def _launch(self, cmd: list[str], log: Any) -> subprocess.Popen:
        # Serialize process creation with stop(): cancellation cannot miss a
        # process created immediately after the operator requests a stop.
        with self._lock:
            self._checkpoint()
            self._process = subprocess.Popen(cmd, cwd=REPO, stdout=log,
                stderr=subprocess.STDOUT, start_new_session=True)
            return self._process

    def _terminate_process(self) -> None:
        with self._lock:
            proc = self._process
            self._process = None
        if proc is None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        # The uv parent may have exited while its scheduler child remains.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=3)

    def _finish_cancel(self, run_id: str) -> None:
        self._terminate_process()
        errors = []
        selector = f"app.kubernetes.io/managed-by=carbon-scheduler,carbon-scheduler/run-id={run_id}"
        for context in CLUSTERS.values():
            try:
                proc = subprocess.run(["kubectl", "--context", context, "--request-timeout=10s",
                    "delete", "jobs", "-n", NAMESPACE, "-l", selector,
                    "--cascade=foreground", "--wait=true", "--timeout=20s", "--ignore-not-found=true"],
                    capture_output=True, text=True, timeout=30)
                if proc.returncode:
                    errors.append(f"{context}: {proc.stderr.strip()[-300:] or 'Job cleanup failed'}")
            except (OSError, subprocess.TimeoutExpired) as exc:
                errors.append(f"{context}: {exc}")
        phase = "cancel_failed" if errors else "cancelled"
        text = "Stop incomplete. Retry force stop. " + "; ".join(errors) if errors else (
            "Live cycle stopped. Its reset and benchmark Jobs were removed. Application deployments and saved receipts are preserved.")
        self._event(phase, text, finished_at=_now().isoformat(), cleanup_errors=errors)
        out = OUT_ROOT / run_id
        out.mkdir(parents=True, exist_ok=True)
        with self._lock:
            snapshot = dict(self._state)
        (out / "cancellation.json").write_text(json.dumps(snapshot, indent=2), encoding="utf-8")

    # ------------------------------------------------------------- internals
    def _event(self, phase: str, text: str, **extra: Any) -> None:
        with self._lock:
            if self._cancel.is_set() and phase not in {"cancelled", "cancel_failed"}:
                raise CycleCancelled()
            self._state["phase"] = phase
            self._state.setdefault("events", []).append({"at": _now().isoformat(), "phase": phase, "text": text})
            self._state.update(extra)
        sys.stderr.write(f"[LiveCycle] {phase}: {text}\n")

    def _run(self, run_id: str, scenario: str | None) -> None:
        out = OUT_ROOT / run_id
        try:
            self._checkpoint()
            out.mkdir(parents=True, exist_ok=True)
            requested_at = _now()
            earliest = _floor_to_slot(requested_at)
            config = yaml.safe_load(BASE_CONFIG.read_text(encoding="utf-8"))
            config["workload"]["earliest_start"] = _iso(earliest)
            config["workload"]["deadline"] = _iso(requested_at + timedelta(minutes=WINDOW_MINUTES))
            config_path = out / "config.yaml"
            config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
            self._event("capturing", "Capturing live carbon intensity and forecast from Electricity Maps",
                        window=[config["workload"]["earliest_start"], config["workload"]["deadline"]])

            trace = out / "carbon-trace.json"
            code = self._call(["uv", "run", "python", "scripts/capture_carbon.py", "--config", str(config_path),
                               "--output", str(trace)], out / "capture.log", timeout=120)
            if code != 0:
                raise RuntimeError(f"carbon capture failed (exit {code}); see {out / 'capture.log'}")

            if scenario == "spike":
                trace = self._apply_spike(trace, out)

            self._event("deciding", "Llama-3 8B proposes a placement; gate v2.1 checks it against the exact program")
            cmd = ["uv", "run", "carbon-scheduler", "--config", str(config_path), "--mode", "hybrid",
                   "--gate-profile", "v2.1", "--target-config", str(TARGETS), "--graph", str(GRAPH),
                   "--replay-trace", str(trace), "--run-root", str(out / "runs"), "--run-id", run_id,
                   "--max-cycles", "1"]
            log = (out / "scheduler.log").open("w", encoding="utf-8")
            proc = self._launch(cmd, log)
            deadline = time.monotonic() + CLI_TIMEOUT_SECONDS
            seen: dict[str, str] = {}
            while proc.poll() is None:
                self._checkpoint()
                if time.monotonic() > deadline:
                    raise RuntimeError("scheduler did not finish within 30 minutes")
                self._watch_jobs(run_id, seen)
                self._cancel.wait(5)
            log.close()
            self._checkpoint()
            self._watch_jobs(run_id, seen)

            summary = summarize_run(out / "runs" / run_id)
            if summary is None:
                tail = (out / "scheduler.log").read_text(encoding="utf-8", errors="replace")[-400:]
                raise RuntimeError(f"scheduler exited {proc.returncode} without a receipt: {tail.strip()}")
            if summary["outcome"] == "executed":
                text = (f"Completed on {summary['cluster']}: P95 {summary['p95_ms']} ms, "
                        f"error rate {summary['error_rate']}, SLO {'passed' if summary['slo_passed'] else 'failed'}")
            else:
                text = f"Cycle ended with outcome '{summary['outcome']}': {summary['failure_reason'] or 'no session started'}"
            self._event("completed", text, receipt=summary, region=summary["final_region"],
                        cluster=summary["cluster"], finished_at=_now().isoformat())
        except CycleCancelled:
            self._finish_cancel(run_id)
        except Exception as exc:  # report every failure on the page
            if self._cancel.is_set():
                self._finish_cancel(run_id)
            else:
                try:
                    self._event("failed", str(exc), finished_at=_now().isoformat())
                except CycleCancelled:
                    # stop() may arrive between the check above and recording
                    # the failure. It must still get its Job cleanup.
                    self._finish_cancel(run_id)
        finally:
            self._terminate_process()
            if 'log' in locals():
                log.close()

    def _apply_spike(self, trace: Path, out: Path) -> Path:
        data = json.loads(trace.read_text(encoding="utf-8"))
        regions = data["snapshots"][0]["regions"]
        current = {r: regions[r]["current"]["carbon_intensity_gco2eq_per_kwh"] for r in regions}
        spiked = min(current, key=current.get)
        other = max(current, key=current.get)
        factor = max(SPIKE_FACTOR, 1.2 * current[other] / current[spiked]) if current[spiked] > 0 else SPIKE_FACTOR
        for snapshot in data["snapshots"]:
            info = snapshot["regions"][spiked]
            info["current"]["carbon_intensity_gco2eq_per_kwh"] = round(info["current"]["carbon_intensity_gco2eq_per_kwh"] * factor, 1)
            for point in info.get("forecast") or []:
                point["carbon_intensity_gco2eq_per_kwh"] = round(point["carbon_intensity_gco2eq_per_kwh"] * factor, 1)
        spiked_trace = out / "carbon-trace-scenario.json"
        spiked_trace.write_text(json.dumps(data, indent=2), encoding="utf-8")
        scenario = {
            "name": "spike",
            "region": spiked,
            "zone": regions[spiked].get("reported_zone"),
            "factor": round(factor, 2),
            "live_value": current[spiked],
            "scenario_value": round(current[spiked] * factor, 1),
            "other_region": other,
            "other_value": current[other],
            "note": "Live capture in carbon-trace.json; the scheduler decided on carbon-trace-scenario.json, "
                    "where this region's current and forecast intensities were multiplied by the factor. "
                    "raw_source_hash values refer to the unmodified capture.",
        }
        (out / "scenario.json").write_text(json.dumps(scenario, indent=2), encoding="utf-8")
        self._event("capturing", f"Scenario: {spiked} ({scenario['zone']}) spiked from {current[spiked]:g} to "
                                 f"{scenario['scenario_value']:g} gCO₂/kWh (×{scenario['factor']:g}, current and forecast); "
                                 f"{other} stays at its live {current[other]:g}", scenario=scenario)
        return spiked_trace

    def _watch_jobs(self, run_id: str, seen: dict[str, str]) -> None:
        """Report this cycle's Jobs as they appear and progress on either cluster."""

        for region, context in CLUSTERS.items():
            self._checkpoint()
            try:
                proc = subprocess.run(
                    ["kubectl", "--context", context, "--request-timeout=8s", "get", "jobs", "-n", NAMESPACE, "-o", "json"],
                    capture_output=True, text=True, timeout=12)
                items = json.loads(proc.stdout).get("items", []) if proc.returncode == 0 else []
            except (subprocess.TimeoutExpired, json.JSONDecodeError):
                continue
            for job in sorted(items, key=lambda j: not j["metadata"]["name"].startswith("reset")):
                name = job["metadata"]["name"]
                if run_id not in name:
                    continue
                status = job.get("status", {})
                kind = "reset" if name.startswith("reset") else "benchmark"
                state = "succeeded" if status.get("succeeded") else "failed" if status.get("failed") else "running"
                key = f"{kind}:{state}"
                if seen.get(name) == key:
                    continue
                seen[name] = key
                phase = {"reset": "resetting", "benchmark": "benchmarking"}[kind]
                if state == "running":
                    text = {"reset": f"Fixture-reset Job created on {context}",
                            "benchmark": f"wrk2 benchmark Job running on {context}: compose-post, 10 req/s for 60 s"}[kind]
                else:
                    text = f"{kind.capitalize()} Job {state} on {context}"
                with self._lock:
                    jobs = dict(self._state.get("jobs") or {})
                    jobs[name] = {"cluster": context, "kind": kind, "state": state}
                self._event(phase, text, region=region, cluster=context, jobs=jobs)

    def _call(self, cmd: list[str], log: Path, timeout: int) -> int:
        with log.open("w", encoding="utf-8") as handle:
            proc = self._launch(cmd, handle)
            deadline = time.monotonic() + timeout
            try:
                while proc.poll() is None:
                    self._checkpoint()
                    if time.monotonic() > deadline:
                        return -1
                    self._cancel.wait(0.2)
                self._checkpoint()
                return proc.returncode
            finally:
                self._terminate_process()
