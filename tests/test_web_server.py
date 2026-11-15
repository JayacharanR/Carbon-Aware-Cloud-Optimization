"""Request policy of the dashboard server.

The control endpoints start real Jobs on AKS, so these tests pin who may call
them and what the static handler is willing to serve.  Nothing here reaches
Electricity Maps, kubectl or a cluster.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from web import live_cycle, server


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def start(self, cluster_states, scenario=None):
        self.calls.append(("start", scenario))
        return True, "web-test"

    def stop(self):
        self.calls.append(("stop",))
        return True, "Stopping the live cycle"

    def reset(self):
        self.calls.append(("reset",))
        return True, "reset"

    def state(self):
        return {"current": {"phase": "idle"}, "history": []}


class FakeClusters:
    def get(self, fresh: bool = False):
        return {key: {"context": key, "state": "ready"} for key in ("primary", "secondary")}


@pytest.fixture
def dashboard():
    settings = server.Settings()
    httpd = server.DashboardServer(
        ("127.0.0.1", 0), settings, carbon_poller=server.LiveCarbonPoller(settings),
        cluster_cache=FakeClusters(), cycle_runner=FakeRunner())
    httpd.RequestHandlerClass.log_message = lambda *args: None
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def call(httpd, method: str, path: str, headers: dict[str, str] | None = None):
    connection = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)
    try:
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.headers, response.read()
    finally:
        connection.close()


def origin(httpd) -> str:
    return f"http://127.0.0.1:{httpd.server_port}"


# ---------------------------------------------------------------- control


@pytest.mark.parametrize("headers", [
    {"Origin": "https://evil.example"},
    {"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
    {"Sec-Fetch-Site": "cross-site"},
    {"Origin": "http://127.0.0.1:1"},   # same host, another port, is another origin
    {"Origin": "null"},
])
@pytest.mark.parametrize("path", ["/api/cycle/run", "/api/cycle/run?scenario=spike", "/api/cycle/stop", "/api/cycle/reset"])
def test_another_site_cannot_control_live_cycles(dashboard, headers, path):
    status, _, body = call(dashboard, "POST", path, headers)
    assert status == 403
    assert json.loads(body)["error"] == "forbidden"
    assert dashboard.cycle_runner.calls == []


def test_the_page_itself_and_local_tools_can_control_live_cycles(dashboard):
    same_origin = {"Origin": origin(dashboard), "Sec-Fetch-Site": "same-origin"}
    assert call(dashboard, "POST", "/api/cycle/run?scenario=spike", same_origin)[0] == 202
    assert call(dashboard, "POST", "/api/cycle/stop")[0] == 202  # no Origin: curl on this machine
    assert dashboard.cycle_runner.calls == [("start", "spike"), ("stop",)]


@pytest.mark.parametrize("method,path", [("GET", "/api/cycle"), ("GET", "/"), ("HEAD", "/index.html"),
                                         ("POST", "/api/cycle/run")])
def test_requests_for_an_unknown_host_name_are_refused(dashboard, method, path):
    """A rebinding page reaches the server under its own name, never under ours."""
    status, _, _ = call(dashboard, method, path, {"Host": "evil.example"})
    assert status == 403
    assert dashboard.cycle_runner.calls == []


def test_extra_host_names_must_be_listed_explicitly(dashboard):
    dashboard.allowed_hosts = dashboard.allowed_hosts | {"demo.lan"}
    assert call(dashboard, "GET", "/api/status", {"Host": "demo.lan:8000"})[0] == 200
    assert call(dashboard, "GET", "/api/status", {"Host": "[::1]:8000"})[0] == 200
    assert call(dashboard, "GET", "/api/status", {"Host": "demo.lan.evil.example"})[0] == 403


def test_other_machines_cannot_control_live_cycles_unless_allowed(dashboard, monkeypatch):
    monkeypatch.setattr(server.CarbonSchedulerWebHandler, "_client_is_local", lambda self: False)
    assert call(dashboard, "POST", "/api/cycle/run")[0] == 403
    assert call(dashboard, "GET", "/api/cycle")[0] == 200  # reading stays available
    assert dashboard.cycle_runner.calls == []
    dashboard.allow_remote_control = True
    assert call(dashboard, "POST", "/api/cycle/run")[0] == 202


# ----------------------------------------------------------------- static


@pytest.mark.parametrize("path", [
    "/server.py", "/live_cycle.py", "/js/", "/data/", "/__pycache__/", "/.env", "/js/.hidden.js",
    "/%2e%2e/.env", "/../pyproject.toml", "/js/missing.js", "/README.md",
])
def test_only_page_assets_are_served(dashboard, path):
    assert call(dashboard, "GET", path)[0] == 404


def test_page_assets_carry_security_headers_and_revalidate(dashboard):
    status, headers, body = call(dashboard, "GET", "/")
    assert status == 200 and b"<html" in body
    assert headers["Content-Type"].startswith("text/html")
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert "script-src 'self'" in headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert headers["Cache-Control"] == "no-cache"

    status, headers, _ = call(dashboard, "GET", "/js/app.js?v=4.1")
    assert status == 200 and headers["Content-Type"].startswith("application/javascript")
    status, _, body = call(dashboard, "GET", "/js/app.js", {"If-Modified-Since": headers["Last-Modified"]})
    assert status == 304 and body == b""

    status, headers, _ = call(dashboard, "GET", "/api/status")
    assert status == 200 and headers["Cache-Control"] == "no-store"
    assert headers["X-Frame-Options"] == "DENY"


# ------------------------------------------------------------- components


def test_concurrent_status_requests_share_one_kubectl_check(monkeypatch):
    checks = []

    def check(context, namespace):
        checks.append(context)
        time.sleep(0.05)
        return {"context": context, "state": "ready"}

    monkeypatch.setattr(server, "_check_cluster", check)
    cache = server.ClusterStatusCache(server.Settings())
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: cache.get(), range(8)))
    assert len(checks) == 2  # one check of each cluster, not eight
    assert all(result is results[0] for result in results)
    cache.get()
    assert len(checks) == 2  # still cached
    cache.get(fresh=True)
    assert len(checks) == 4  # a run request always looks again


@pytest.mark.parametrize("payload", [
    b'{"zone": "US-MIDA-PJM", "carbonIntensity": "high"}',
    b'{"zone": "US-MIDA-PJM", "carbonIntensity": Infinity}',
    b'{"zone": "US-MIDA-PJM", "carbonIntensity": -1}',
    b'{"zone": "US-MIDA-PJM", "carbonIntensity": true}',
    b'["not", "an", "object"]',
    b"\xff\xfe not text",
])
def test_unusable_provider_answers_become_a_hold_not_a_crash(monkeypatch, payload):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return payload

    monkeypatch.setattr(server, "urlopen", lambda *args, **kwargs: Response())
    poller = server.LiveCarbonPoller(server.Settings(em_api_key="test-key"))
    poller._poll_once()
    state = poller.get_state()
    assert state["status"] == "hold"
    assert state["recommended_region"] is None


def test_settings_come_from_the_environment_once(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text('# comment\nELECTRICITY_MAPS_API_KEY="quoted-key"\nexport WORKLOAD_NAMESPACE=bench\n'
                        "TARGET_PRIMARY_KUBE_CONTEXT='east'\nAZURE_PRIMARY_REGION=from-file\n", encoding="utf-8")
    env = {"AZURE_PRIMARY_REGION": "from-shell"}
    server.load_dotenv(env_file, env)
    settings = server.Settings.from_env(env)
    assert settings.em_api_key == "quoted-key"
    assert settings.namespace == "bench"
    assert settings.kube_context_primary == "east"
    assert settings.primary_region == "from-shell"  # the shell wins over the file
    with pytest.raises(ValueError):
        server.Settings.from_env({"ELECTRICITY_MAPS_API_BASE_URL": "file:///etc/passwd"})


def test_a_half_written_receipt_does_not_break_the_cycle_status(tmp_path, monkeypatch):
    monkeypatch.setattr(live_cycle, "OUT_ROOT", tmp_path)
    good = tmp_path / "web-1" / "runs" / "web-1"
    bad = tmp_path / "web-2" / "runs" / "web-2"
    good.mkdir(parents=True)
    bad.mkdir(parents=True)
    (good / "cycles.jsonl").write_text(json.dumps({"outcome": "executed", "decision_input": {"run_id": "web-1"}}) + "\n")
    (bad / "cycles.jsonl").write_text('{"outcome": "exec')  # the scheduler is still writing this line
    runner = live_cycle.LiveCycleRunner()
    assert [run["run_id"] for run in runner.state()["history"]] == ["web-1"]
    # When the line is complete the run appears; the cached "no receipt" is not kept.
    (bad / "cycles.jsonl").write_text(json.dumps({"outcome": "milp_fallback", "decision_input": {"run_id": "web-2"}}) + "\n")
    assert [run["run_id"] for run in runner.state()["history"]] == ["web-2", "web-1"]


def test_job_polling_and_cleanup_use_the_configured_clusters(tmp_path, monkeypatch):
    monkeypatch.setattr(live_cycle, "OUT_ROOT", tmp_path)
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return type("Result", (), {"returncode": 0, "stdout": '{"items": []}', "stderr": ""})()

    monkeypatch.setattr(live_cycle.subprocess, "run", run)
    runner = live_cycle.LiveCycleRunner(clusters={"east": "ctx-east", "west": "ctx-west"}, namespace="bench")
    runner._state = {"phase": "deciding", "run_id": "web-9", "events": []}
    runner._watch_jobs("web-9", {})
    runner._finish_cancel("web-9")
    assert [c[c.index("--context") + 1] for c in commands] == ["ctx-east", "ctx-west", "ctx-east", "ctx-west"]
    assert all(c[c.index("-n") + 1] == "bench" for c in commands)
    assert all("carbon-scheduler/run-id=web-9" in c[c.index("-l") + 1] for c in commands)
