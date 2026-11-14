#!/usr/bin/env python3
"""Showcase web server for Carbon-Aware Cloud Optimization.

Serves the dashboard and three kinds of data, kept strictly apart:

* live      - /api/live-carbon: Electricity Maps latest intensity for the two
              grid zones (US-MIDA-PJM -> eastus2, US-NW-BPAT -> westus2), with
              the same age and zone-identity checks the scheduler's gate v2.1
              applies; /api/cluster-status: a kubectl check of both AKS clusters.
* recorded  - web/data/evidence.json: dated results exported from the
              experiment receipts by scripts/export_web_evidence.py.
* live runs - POST /api/cycle/run starts one real scheduling cycle (live_cycle.py):
              Llama-3 proposal, gate v2.1, and real reset and wrk2 Jobs on the
              chosen AKS cluster; GET /api/cycle reports its progress and receipts.
* simulated - the gate sandbox and decision walkthrough; the server never
              claims that a simulated action ran.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_cycle import LiveCycleRunner  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = REPO_ROOT / "web"
EVIDENCE_FILE = WEB_DIR / "data" / "evidence.json"


def _load_dotenv() -> None:
    """Load .env before the module-level settings below are read."""
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()

EM_API_KEY = os.environ.get("ELECTRICITY_MAPS_API_KEY", "").strip()
EM_API_BASE = os.environ.get("ELECTRICITY_MAPS_API_BASE_URL", "https://api.electricitymaps.com/v4").rstrip("/")

PRIMARY_REGION = os.environ.get("AZURE_PRIMARY_REGION", "eastus2")
SECONDARY_REGION = os.environ.get("AZURE_SECONDARY_REGION", "westus2")
# The *_EASTUS/*_WESTUS2 names are the older spellings of the same settings.
ZONE_PRIMARY = os.environ.get("ELECTRICITY_MAPS_ZONE_PRIMARY", os.environ.get("ELECTRICITY_MAPS_ZONE_EASTUS", "US-MIDA-PJM"))
ZONE_SECONDARY = os.environ.get("ELECTRICITY_MAPS_ZONE_SECONDARY", os.environ.get("ELECTRICITY_MAPS_ZONE_WESTUS2", "US-NW-BPAT"))

POLL_INTERVAL_SECONDS = 300          # Electricity Maps publishes hourly; 5 min is plenty
MAX_OBSERVATION_AGE_SECONDS = 7200   # the scheduler's gate v2.1 data-age limit (2 h)

KUBE_CONTEXT_PRIMARY = os.environ.get("TARGET_PRIMARY_KUBE_CONTEXT", "aks-primary-eastus2")
KUBE_CONTEXT_SECONDARY = os.environ.get("TARGET_SECONDARY_KUBE_CONTEXT", "aks-secondary-westus2")
BENCHMARK_NAMESPACE = os.environ.get("WORKLOAD_NAMESPACE", "benchmark")
CLUSTER_CACHE_SECONDS = 30


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Live carbon feed
# ---------------------------------------------------------------------------

class LiveCarbonPoller:
    """Polls Electricity Maps and reports only what was actually observed.

    A recommendation (the lower-intensity region) is given only when both zones
    returned a value for the zone that was asked for and the observation is no
    older than the scheduler's data-age limit. Otherwise the state is "hold",
    which is what the scheduler itself would do with such inputs.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._zones: dict[str, dict[str, Any]] = {
            "primary": self._empty(PRIMARY_REGION, ZONE_PRIMARY),
            "secondary": self._empty(SECONDARY_REGION, ZONE_SECONDARY),
        }
        self._last_poll_at: str | None = None
        self._thread = threading.Thread(target=self._poll_loop, name="LiveCarbonPoller", daemon=True)

    @staticmethod
    def _empty(region: str, zone: str) -> dict[str, Any]:
        return {"region": region, "zone_requested": zone, "zone_returned": None, "intensity": None,
                "observed_at": None, "fetched_at": None, "is_estimated": None, "estimation_method": None,
                "emission_factor_type": None, "error": None if EM_API_KEY else "ELECTRICITY_MAPS_API_KEY is not set"}

    def start(self) -> None:
        self._thread.start()

    def _fetch_zone(self, zone: str) -> dict[str, Any]:
        if not EM_API_KEY:
            return {"error": "ELECTRICITY_MAPS_API_KEY is not set"}
        url = f"{EM_API_BASE}/carbon-intensity/latest?zone={zone}"
        req = Request(url, headers={"auth-token": EM_API_KEY, "User-Agent": "carbon-scheduler-showcase"})
        try:
            with urlopen(req, timeout=12) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            return {"error": f"Electricity Maps returned HTTP {exc.code}"}
        except (URLError, TimeoutError, OSError) as exc:
            return {"error": f"Electricity Maps unreachable ({getattr(exc, 'reason', exc)})"}
        except json.JSONDecodeError:
            return {"error": "Electricity Maps returned an unreadable response"}
        if payload.get("carbonIntensity") is None:
            return {"error": "Electricity Maps returned no intensity for this zone"}
        return {
            "zone_returned": payload.get("zone"),
            "intensity": float(payload["carbonIntensity"]),
            "observed_at": payload.get("datetime"),
            "fetched_at": _now().isoformat(),
            "is_estimated": payload.get("isEstimated"),
            "estimation_method": payload.get("estimationMethod"),
            "emission_factor_type": payload.get("emissionFactorType"),
            "error": None,
        }

    def _poll_once(self) -> None:
        results = {key: self._fetch_zone(z["zone_requested"]) for key, z in self._zones.items()}
        with self._lock:
            for key, fresh in results.items():
                if fresh.get("error") is None:
                    self._zones[key].update(fresh)
                else:
                    # Keep the last observation, but say that this poll failed.
                    self._zones[key]["error"] = fresh["error"]
            self._last_poll_at = _now().isoformat()
        state = self.get_state()
        sys.stderr.write(f"[CarbonPoller] {state['last_poll_at']} status={state['status']} "
                         f"{PRIMARY_REGION}={state['primary']['intensity']} {SECONDARY_REGION}={state['secondary']['intensity']}"
                         f"{' reason=' + state['hold_reason'] if state['hold_reason'] else ''}\n")

    def _poll_loop(self) -> None:
        while True:
            try:
                self._poll_once()
            except Exception as exc:  # never let the poller thread die
                sys.stderr.write(f"[CarbonPoller] unexpected error: {exc}\n")
            time.sleep(POLL_INTERVAL_SECONDS)

    def get_state(self) -> dict[str, Any]:
        with self._lock:
            zones = {k: dict(v) for k, v in self._zones.items()}
            last_poll = self._last_poll_at
        now = _now()
        problems = []
        for key, z in zones.items():
            observed = _parse_time(z["observed_at"])
            z["age_seconds"] = round((now - observed).total_seconds()) if observed else None
            if z["intensity"] is None:
                problems.append(f"no observation for {z['region']} ({z['error'] or 'not polled yet'})")
            elif z["zone_returned"] != z["zone_requested"]:
                problems.append(f"{z['region']}: provider returned zone {z['zone_returned']!r}, expected {z['zone_requested']!r}")
            elif z["age_seconds"] is not None and z["age_seconds"] > MAX_OBSERVATION_AGE_SECONDS:
                problems.append(f"{z['region']}: observation is {z['age_seconds'] // 60} min old (limit {MAX_OBSERVATION_AGE_SECONDS // 60} min)")
            elif z["error"]:
                problems.append(f"{z['region']}: latest poll failed ({z['error']}); showing the previous observation")
        stale_only = all("previous observation" in p for p in problems) and problems
        p, s = zones["primary"], zones["secondary"]
        status = "ok" if not problems else ("degraded" if stale_only else "hold")
        recommended = reduction = None
        if status != "hold":
            if s["intensity"] < p["intensity"]:
                recommended, reduction = s["region"], round((p["intensity"] - s["intensity"]) / p["intensity"] * 100, 1)
            else:
                recommended, reduction = p["region"], 0.0
        return {
            "status": status, "hold_reason": "; ".join(problems) or None,
            "primary": p, "secondary": s,
            "recommended_region": recommended, "reduction_pct": reduction,
            "last_poll_at": last_poll, "poll_interval_seconds": POLL_INTERVAL_SECONDS,
            "max_observation_age_seconds": MAX_OBSERVATION_AGE_SECONDS,
            "signal": "Electricity Maps latest carbon intensity (lifecycle, location-based, gCO2eq/kWh)",
            "note": "The recommendation is the lower-intensity region right now. It is not a scheduler decision and nothing is deployed.",
        }


# ---------------------------------------------------------------------------
# Cluster status
# ---------------------------------------------------------------------------

def _classify_kubectl_error(stderr: str) -> str:
    text = stderr.lower()
    if "no such host" in text or "lookup" in text or "dial tcp" in text:
        return "API server not reachable (DNS lookup failed); the cluster is probably stopped"
    if "i/o timeout" in text or "timed out" in text or "deadline exceeded" in text:
        return "API server did not answer in time"
    if "unauthorized" in text or "forbidden" in text or "credentials" in text:
        return "credentials rejected by the API server"
    if "context" in text and ("not found" in text or "does not exist" in text):
        return "kubectl context is not configured on this machine"
    if "namespaces" in text and "not found" in text:
        return f"namespace {BENCHMARK_NAMESPACE!r} does not exist"
    return "kubectl check failed"


def _check_cluster(context: str) -> dict[str, Any]:
    result: dict[str, Any] = {"context": context, "namespace": BENCHMARK_NAMESPACE, "state": "unknown",
                              "running": None, "total": None, "reason": None, "detail": None}
    try:
        proc = subprocess.run(["kubectl", "get", "pods", "-n", BENCHMARK_NAMESPACE, "--context", context,
                               "--request-timeout=8s", "-o", "json"], capture_output=True, text=True, timeout=12)
    except FileNotFoundError:
        result.update(state="unknown", reason="kubectl is not installed on the web host")
        return result
    except subprocess.TimeoutExpired:
        result.update(state="unreachable", reason="API server did not answer in time")
        return result
    if proc.returncode != 0:
        result.update(state="unreachable", reason=_classify_kubectl_error(proc.stderr),
                      detail=proc.stderr.strip().splitlines()[-1][:240] if proc.stderr.strip() else None)
        return result
    try:
        items = json.loads(proc.stdout).get("items", [])
    except json.JSONDecodeError:
        result.update(state="unknown", reason="kubectl returned unreadable output")
        return result
    # Completed one-off Jobs (fixture reset, benchmark) are not part of the stamp.
    pods = [p for p in items if p.get("status", {}).get("phase") != "Succeeded"]
    running = sum(1 for p in pods if p.get("status", {}).get("phase") == "Running" and all(
        c.get("ready") for c in p.get("status", {}).get("containerStatuses", []) or [{}]))
    result.update(running=running, total=len(pods))
    if not pods:
        result.update(state="empty", reason=f"no pods in namespace {BENCHMARK_NAMESPACE!r}")
    elif running == len(pods):
        result.update(state="ready")
    else:
        result.update(state="degraded", reason=f"{len(pods) - running} of {len(pods)} pods not running and ready")
    return result


class ClusterStatusCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value: dict[str, Any] | None = None
        self._at = 0.0

    def get(self, fresh: bool = False) -> dict[str, Any]:
        with self._lock:
            if not fresh and self._value is not None and time.monotonic() - self._at < CLUSTER_CACHE_SECONDS:
                return self._value
        with ThreadPoolExecutor(max_workers=2) as pool:
            primary, secondary = pool.map(_check_cluster, [KUBE_CONTEXT_PRIMARY, KUBE_CONTEXT_SECONDARY])
        value = {"primary": primary, "secondary": secondary, "checked_at": _now().isoformat(),
                 "note": "Readiness of the two AKS stamps as seen by kubectl from the web host."}
        with self._lock:
            self._value, self._at = value, time.monotonic()
        return value


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class CarbonSchedulerWebHandler(SimpleHTTPRequestHandler):
    carbon_poller: LiveCarbonPoller
    cluster_cache: ClusterStatusCache
    cycle_runner: LiveCycleRunner

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def do_GET(self) -> None:
        if self.path.startswith("/api/"):
            self.handle_api()
        else:
            super().do_GET()

    def do_POST(self) -> None:
        path = self.path.split("?")[0]
        if path == "/api/cycle/stop":
            stopped, detail = self.cycle_runner.stop()
            self.send_json({"stopping": stopped, "reason": detail},
                status=HTTPStatus.ACCEPTED if stopped else HTTPStatus.CONFLICT)
        elif path == "/api/cycle/reset":
            reset, detail = self.cycle_runner.reset()
            self.send_json({"reset": reset, "reason": detail},
                status=HTTPStatus.OK if reset else HTTPStatus.CONFLICT)
        elif path == "/api/cycle/run":
            query = parse_qs(urlsplit(self.path).query)
            clusters = self.cluster_cache.get(fresh=True)
            started, detail = self.cycle_runner.start(
                {clusters[k]["context"]: clusters[k]["state"] for k in ("primary", "secondary")},
                scenario=(query.get("scenario") or [None])[0])
            if started:
                self.send_json({"started": True, "run_id": detail}, status=HTTPStatus.ACCEPTED)
            else:
                self.send_json({"started": False, "reason": detail}, status=HTTPStatus.CONFLICT)
        else:
            self.send_json({"error": "API endpoint not found"}, status=HTTPStatus.NOT_FOUND)

    def end_headers(self) -> None:
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        super().end_headers()

    def handle_api(self) -> None:
        path = self.path.split("?")[0]
        if path == "/api/status":
            self.send_json({
                "server": "ok",
                "gate_profile": "v2.1",
                "live_carbon_configured": bool(EM_API_KEY),
                "evidence_available": EVIDENCE_FILE.exists(),
                "clusters": {"primary": KUBE_CONTEXT_PRIMARY, "secondary": KUBE_CONTEXT_SECONDARY},
                "workload": "DeathStarBench Social Network",
                "version": "3.0.0",
            })
        elif path == "/api/live-carbon":
            self.send_json(self.carbon_poller.get_state())
        elif path == "/api/cluster-status":
            self.send_json(self.cluster_cache.get())
        elif path == "/api/cycle":
            self.send_json(self.cycle_runner.state())
        elif path in ("/api/real-pilot", "/api/summary"):
            self.send_json({"error": "withdrawn",
                            "reason": "The September 2026 pilot and the demo run summary are not valid measurements; "
                                      "see artifacts/withdrawn-web-data/README.md. Recorded results: /data/evidence.json."},
                           status=HTTPStatus.GONE)
        else:
            self.send_json({"error": "API endpoint not found"}, status=HTTPStatus.NOT_FOUND)

    def send_json(self, data: Any, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(data, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        sys.stderr.write(f"[{self.log_date_time_string()}] {format % args}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Carbon scheduler showcase web server")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    mimetypes.add_type("application/javascript", ".js")
    mimetypes.add_type("text/css", ".css")
    mimetypes.add_type("application/json", ".json")

    if not EM_API_KEY:
        sys.stderr.write("[Server] ELECTRICITY_MAPS_API_KEY is not set; the live carbon panel will show 'unavailable'.\n")
    if not EVIDENCE_FILE.exists():
        sys.stderr.write("[Server] web/data/evidence.json is missing; run scripts/export_web_evidence.py.\n")

    poller = LiveCarbonPoller()
    CarbonSchedulerWebHandler.carbon_poller = poller
    CarbonSchedulerWebHandler.cluster_cache = ClusterStatusCache()
    CarbonSchedulerWebHandler.cycle_runner = LiveCycleRunner()
    poller.start()

    httpd = ThreadingHTTPServer((args.host, args.port), CarbonSchedulerWebHandler)
    print(f"Carbon-aware scheduler showcase: http://localhost:{args.port}/")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.server_close()


if __name__ == "__main__":
    main()
