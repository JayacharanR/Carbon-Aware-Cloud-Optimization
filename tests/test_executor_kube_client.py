"""Drive the executor through the real ``kubernetes`` client against a stub API server.

The other executor tests use fake API objects.  This one exercises the
installed client library over HTTP (kubeconfig loading, request serialisation,
``_request_timeout``, response decoding), so a client or urllib3 upgrade that
breaks the calls the executor relies on fails here instead of on a cluster.
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

pytest.importorskip("kubernetes")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from carbon_scheduler.executor import KubernetesBenchmarkExecutor, KubernetesRegionTarget  # noqa: E402
from carbon_scheduler.schemas import ActionProposal, ActionType, DecisionMode  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
TEMPLATES = REPO / "deploy" / "deathstarbench"
NAMESPACE = "benchmark"
JOBS = f"/apis/batch/v1/namespaces/{NAMESPACE}/jobs"
CORE = f"/api/v1/namespaces/{NAMESPACE}"

WRK_SUMMARY = """Running 1m test @ http://nginx-thrift.benchmark.svc.cluster.local:8080
  2 threads and 2 connections
       4.771     0.950000          620        20.00
  653 requests in 1.00m, 137.10KB read
Requests/sec:     10.88
"""


class StubApiServer(ThreadingHTTPServer):
    """Just enough of the Kubernetes API for one reset and one benchmark Job."""

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), StubHandler)
        self.jobs: dict[str, dict] = {}
        self.requests: list[tuple[str, str]] = []


class StubHandler(BaseHTTPRequestHandler):
    server: StubApiServer

    def log_message(self, *args) -> None:  # keep test output quiet
        pass

    def _reply(self, status: int, body, content_type: str = "application/json") -> None:
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        self.server.requests.append(("GET", path))
        if path == f"{CORE}/endpoints/nginx-thrift":
            self._reply(200, {"kind": "Endpoints", "apiVersion": "v1", "metadata": {"name": "nginx-thrift"},
                              "subsets": [{"addresses": [{"ip": "10.0.0.7"}]}]})
        elif path.startswith(JOBS + "/"):
            job = self.server.jobs.get(path.rsplit("/", 1)[1])
            if job is None:
                self._reply(404, {"kind": "Status", "apiVersion": "v1", "status": "Failure", "code": 404})
            else:
                self._reply(200, {**job, "status": {"succeeded": 1}})
        elif path == f"{CORE}/pods":
            self._reply(200, {"kind": "PodList", "apiVersion": "v1", "metadata": {},
                              "items": [{"metadata": {"name": "job-pod"}}]})
        elif path == f"{CORE}/pods/job-pod/log":
            self._reply(200, WRK_SUMMARY, "text/plain")
        else:
            self._reply(404, {"kind": "Status", "code": 404})

    def do_POST(self) -> None:
        self.server.requests.append(("POST", self.path))
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == JOBS:
            self.server.jobs[body["metadata"]["name"]] = body
            self._reply(201, body)
        else:
            self._reply(404, {"kind": "Status", "code": 404})


@pytest.fixture
def api_server():
    server = StubApiServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_executor_runs_reset_then_benchmark_through_the_real_client(api_server, tmp_path) -> None:
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text(yaml.safe_dump({
        "apiVersion": "v1", "kind": "Config", "current-context": "stub",
        "clusters": [{"name": "stub", "cluster": {"server": f"http://127.0.0.1:{api_server.server_port}"}}],
        "users": [{"name": "stub", "user": {}}],
        "contexts": [{"name": "stub", "context": {"cluster": "stub", "user": "stub"}}],
    }), encoding="utf-8")
    target = KubernetesRegionTarget(
        region="westus2", cluster_name="aks-secondary-westus2", kube_context="stub", namespace=NAMESPACE,
        frontend_service="nginx-thrift", reset_job_template=TEMPLATES / "reset-job.yaml",
        benchmark_job_template=TEMPLATES / "benchmark-job.yaml", kubeconfig_path=kubeconfig,
        template_values={
            "DSB_TOOLS_IMAGE": "registry.example/dsb-tools:test", "GRAPH_DATASET": "socfb-Reed98",
            "FRONTEND_HOST": "nginx-thrift", "TARGET_URL": "http://nginx-thrift:8080/wrk2-api/post/compose",
            "ACTIVE_DEADLINE_SECONDS": "600", "WRK_THREADS": "2", "WRK_CONNECTIONS": "2", "WRK_DURATION": "60s",
            "REQUESTS_PER_SECOND": "10", "LOADGEN_CPU_REQUEST": "100m", "LOADGEN_MEMORY_REQUEST": "128Mi",
        },
    )
    executor = KubernetesBenchmarkExecutor(
        {"westus2": target}, job_timeout_seconds=20, poll_interval_seconds=0.01, artifact_root=tmp_path / "logs")
    action = ActionProposal(
        action_type=ActionType.RUN_IN_REGION, target_region="westus2", target_service="nginx-thrift",
        workload_profile="compose-post", expected_p95_latency_ms=100, self_reported_confidence=1,
        rationale="test", model_name="milp", prompt_version="n/a", request_id="req-1")

    receipt = executor.execute(
        action=action, mode=DecisionMode.MILP_ONLY, run_id="run-1", cycle_id="run-1-cycle-0001",
        carbon_snapshot_id="snap-1", config_hash="0123456789abcdef", fallback_triggered=False)

    assert receipt.execution_failure_reason is None
    assert receipt.actual_p95_latency_ms == pytest.approx(4.771)
    assert receipt.actual_error_rate == 0.0
    assert receipt.cluster_name == "aks-secondary-westus2"
    reset_name, benchmark_name = api_server.jobs
    assert reset_name.startswith("reset-") and benchmark_name.startswith("benchmark-")
    assert receipt.job_name == benchmark_name
    created = api_server.jobs[benchmark_name]
    assert created["metadata"]["labels"]["carbon-scheduler/run-id"] == "run-1"
    assert created["spec"]["activeDeadlineSeconds"] == 600
    assert [m for m, p in api_server.requests if p == JOBS] == ["POST", "POST"]
    assert (tmp_path / "logs" / "run-1-cycle-0001-benchmark.log").read_text(encoding="utf-8") == WRK_SUMMARY
