from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from urllib3.exceptions import ReadTimeoutError  # noqa: E402

from carbon_scheduler import executor as executor_module  # noqa: E402
from carbon_scheduler.executor import (  # noqa: E402
    API_TIMEOUT,
    KubernetesBenchmarkExecutor,
    KubernetesRegionTarget,
    benchmark_script_error,
    parse_benchmark_output,
)
from carbon_scheduler.schemas import ActionProposal, ActionType, DecisionMode  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
# The templates live in the real repository even when tests run from a copy.
TEMPLATES = next(
    p for p in (REPO / "deploy/deathstarbench", Path.cwd() / "deploy/deathstarbench") if p.exists()
)

CLEAN_SUMMARY = """Running 1m test @ http://nginx-thrift.benchmark.svc.cluster.local:8080
  2 threads and 2 connections
 50.000%    2.54ms
 90.000%    3.72ms
 99.000%   13.85ms
       4.771     0.950000          620        20.00
  653 requests in 1.00m, 137.10KB read
Requests/sec:     10.88
Transfer/sec:      2.28KB
"""

LUA_FAILURE = """/workload/traffic.lua: /workload/traffic.lua:1: module 'socket' not found:
\tno field package.preload['socket']
Running 1m test @ http://nginx-thrift.benchmark.svc.cluster.local:8080
 50.000%   87.00us
       0.173     0.950000          620        20.00
  651 requests in 1.00m, 1.92MB read
  Socket errors: connect 0, read 0, write 0, timeout 35
Requests/sec:     10.85
"""


class ParserTests(unittest.TestCase):
    def test_complete_summary_without_error_lines_means_zero_errors(self) -> None:
        p95, error_rate = parse_benchmark_output(CLEAN_SUMMARY)
        self.assertAlmostEqual(p95, 4.771)
        self.assertEqual(error_rate, 0.0)

    def test_socket_errors_are_counted(self) -> None:
        _, error_rate = parse_benchmark_output(LUA_FAILURE)
        self.assertAlmostEqual(error_rate, 35 / 651)

    def test_truncated_output_leaves_error_rate_unknown(self) -> None:
        truncated = CLEAN_SUMMARY.split("Requests/sec")[0]
        self.assertIsNone(parse_benchmark_output(truncated)[1])

    def test_script_load_failure_is_detected(self) -> None:
        self.assertIn("module 'socket' not found", benchmark_script_error(LUA_FAILURE))
        self.assertIsNone(benchmark_script_error(CLEAN_SUMMARY))


class _ApiException(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


def _timeout() -> ReadTimeoutError:
    return ReadTimeoutError(None, "/apis/batch/v1", "Read timed out.")


class FakeBatch:
    """Jobs complete as soon as they are created; reads can be made to time out."""

    def __init__(self, read_timeouts: int = 0) -> None:
        self.jobs: dict[str, dict] = {}
        self.read_timeouts = read_timeouts
        self.timeouts_seen: list[object] = []

    def read_namespaced_job(self, name: str, namespace: str, _request_timeout=None):
        self.timeouts_seen.append(_request_timeout)
        if self.read_timeouts and name in self.jobs:
            self.read_timeouts -= 1
            raise _timeout()
        if name not in self.jobs:
            raise _ApiException(404)
        return SimpleNamespace(status=SimpleNamespace(succeeded=1, failed=0))

    def create_namespaced_job(self, namespace: str, body: dict, _request_timeout=None):
        self.timeouts_seen.append(_request_timeout)
        self.jobs[body["metadata"]["name"]] = body


class FakeCore:
    def __init__(self, benchmark_log: str) -> None:
        self.benchmark_log = benchmark_log
        self.timeouts_seen: list[object] = []

    def read_namespaced_endpoints(self, name: str, namespace: str, _request_timeout=None):
        self.timeouts_seen.append(_request_timeout)
        return SimpleNamespace(subsets=[SimpleNamespace(addresses=["10.0.0.1"])])

    def list_namespaced_pod(self, namespace: str, label_selector: str, _request_timeout=None):
        self.timeouts_seen.append(_request_timeout)
        job = label_selector.split("=", 1)[1]
        return SimpleNamespace(items=[SimpleNamespace(metadata=SimpleNamespace(name=f"{job}-pod"))])

    def read_namespaced_pod_log(self, name: str, namespace: str, _request_timeout=None):
        self.timeouts_seen.append(_request_timeout)
        return "Succeeded: 37624\n" if name.startswith("reset-") else self.benchmark_log


class ExecutorTests(unittest.TestCase):
    def _run(self, batch: FakeBatch, core: FakeCore):
        target = KubernetesRegionTarget(
            region="eastus2",
            cluster_name="aks-primary-eastus2",
            kube_context="ctx",
            namespace="benchmark",
            frontend_service="nginx-thrift",
            reset_job_template=TEMPLATES / "reset-job.yaml",
            benchmark_job_template=TEMPLATES / "benchmark-job.yaml",
            template_values={
                "DSB_TOOLS_IMAGE": "img", "GRAPH_DATASET": "socfb-Reed98", "ACTIVE_DEADLINE_SECONDS": "900",
                "WRK_THREADS": "2", "WRK_CONNECTIONS": "2", "WRK_DURATION": "60s", "REQUESTS_PER_SECOND": "10",
                "LOADGEN_CPU_REQUEST": "250m", "LOADGEN_MEMORY_REQUEST": "256Mi",
                "FRONTEND_HOST": "nginx-thrift", "FRONTEND_PORT": "8080", "TARGET_URL": "http://nginx-thrift:8080",
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            ex = KubernetesBenchmarkExecutor({"eastus2": target}, poll_interval_seconds=0.001,
                                             artifact_root=Path(tmp) / "logs")
            with mock.patch.object(KubernetesBenchmarkExecutor, "_clients_for",
                                   return_value=(batch, core, _ApiException)):
                action = ActionProposal(
                    request_id="r", action_type=ActionType.RUN_NOW, target_region="eastus2",
                    target_service="nginx-thrift", workload_profile="pilot", expected_p95_latency_ms=120.0,
                    rationale="test", model_name="m", prompt_version="v", self_reported_confidence=0.5,
                )
                return ex.execute(action=action, mode=DecisionMode.STATIC_REFERENCE, run_id="run",
                                  cycle_id="run-cycle-0001", carbon_snapshot_id="s", config_hash="h" * 64,
                                  fallback_triggered=False)

    def test_read_timeouts_are_retried_and_every_call_is_bounded(self) -> None:
        batch, core = FakeBatch(read_timeouts=2), FakeCore(CLEAN_SUMMARY)
        receipt = self._run(batch, core)
        self.assertIsNone(receipt.execution_failure_reason)
        self.assertAlmostEqual(receipt.actual_p95_latency_ms, 4.771)
        self.assertEqual(receipt.actual_error_rate, 0.0)
        self.assertTrue(all(t == API_TIMEOUT for t in batch.timeouts_seen + core.timeouts_seen))

    def test_benchmark_whose_script_failed_to_load_is_refused(self) -> None:
        receipt = self._run(FakeBatch(), FakeCore(LUA_FAILURE))
        self.assertIn("benchmark script failed to load", receipt.execution_failure_reason)
        self.assertIsNone(receipt.actual_p95_latency_ms)
        # the raw log is still kept as evidence
        self.assertTrue(any(p.endswith("-benchmark.log") for p in receipt.benchmark_artifact_paths))


if __name__ == "__main__":
    unittest.main()
