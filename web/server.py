#!/usr/bin/env python3
"""Showcase Web Server for Carbon-Aware Cloud Optimization.

Serves the frontend application and provides read-only telemetry APIs
from repository artifacts, plus a live carbon intensity feed from
Electricity Maps (zones: US-MIDA-PJM for eastus, US-NW-BPAT for westus2).
"""

from __future__ import annotations

import argparse
import csv
import json
import mimetypes
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen
from urllib.error import URLError

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = REPO_ROOT / "web"

# ---------------------------------------------------------------------------
# Electricity Maps zone configuration
# US-MIDA-PJM  → Azure East US  (PJM interconnection, coal/gas heavy)
# US-NW-BPAT   → Azure West US 2 (Bonneville Power, hydro-heavy)
# ---------------------------------------------------------------------------
EM_API_KEY = os.environ.get(
    "ELECTRICITY_MAPS_API_KEY", "em_TTevcp9h2dcfWwhZSxNZEBfwA3BqhXcn"
)
EM_API_BASE = os.environ.get(
    "ELECTRICITY_MAPS_API_BASE_URL", "https://api.electricitymaps.com/v4"
).rstrip("/")

ZONE_EASTUS = os.environ.get("ELECTRICITY_MAPS_ZONE_EASTUS", "US-MIDA-PJM")
ZONE_WESTUS2 = os.environ.get("ELECTRICITY_MAPS_ZONE_WESTUS2", "US-NW-BPAT")

POLL_INTERVAL_SECONDS = 300  # 5 minutes — matches Electricity Maps update cadence

# Primary AKS context/cluster names from .env
KUBE_CONTEXT_PRIMARY = os.environ.get("TARGET_PRIMARY_KUBE_CONTEXT", "aks-primary-eastus")
KUBE_CONTEXT_SECONDARY = os.environ.get("TARGET_SECONDARY_KUBE_CONTEXT", "aks-secondary-westus2")
BENCHMARK_NAMESPACE = os.environ.get("WORKLOAD_NAMESPACE", "benchmark")


# ---------------------------------------------------------------------------
# Live Carbon Poller — runs in a daemon thread
# ---------------------------------------------------------------------------

class LiveCarbonPoller:
    """Polls Electricity Maps every POLL_INTERVAL_SECONDS and caches results."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Seed with last known good values so the website is never empty
        self._state: dict[str, Any] = {
            "eastus": {
                "zone": ZONE_EASTUS,
                "intensity": None,
                "updated_at": None,
                "source": "pending",
            },
            "westus2": {
                "zone": ZONE_WESTUS2,
                "intensity": None,
                "updated_at": None,
                "source": "pending",
            },
            "decided_region": None,
            "carbon_reduction_pct": None,
            "last_poll_at": None,
            "poll_error": None,
        }
        self._thread = threading.Thread(
            target=self._poll_loop, name="LiveCarbonPoller", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def get_state(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def _fetch_zone(self, zone: str) -> dict[str, Any] | None:
        url = f"{EM_API_BASE}/carbon-intensity/latest?zone={zone}"
        req = Request(url, headers={"auth-token": EM_API_KEY, "User-Agent": "Mozilla/5.0"})
        try:
            with urlopen(req, timeout=12) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            intensity = payload.get("carbonIntensity")
            dt = payload.get("datetime") or payload.get("updatedAt")
            if intensity is None:
                return None
            return {
                "zone": zone,
                "intensity": float(intensity),
                "updated_at": dt,
                "source": "live",
            }
        except (URLError, json.JSONDecodeError, KeyError, TypeError) as exc:
            sys.stderr.write(f"[CarbonPoller] zone={zone} error: {exc}\n")
            return None

    def _poll_once(self) -> None:
        east = self._fetch_zone(ZONE_EASTUS)
        west = self._fetch_zone(ZONE_WESTUS2)
        now_iso = datetime.now(timezone.utc).isoformat()

        with self._lock:
            if east:
                self._state["eastus"] = east
            if west:
                self._state["westus2"] = west

            self._state["last_poll_at"] = now_iso

            # Routing decision: pick the lower-intensity region
            e_int = self._state["eastus"].get("intensity")
            w_int = self._state["westus2"].get("intensity")

            if e_int is not None and w_int is not None:
                if w_int < e_int:
                    decided = "westus2"
                    reduction_pct = round((e_int - w_int) / e_int * 100, 1)
                else:
                    decided = "eastus"
                    reduction_pct = 0.0
                self._state["decided_region"] = decided
                self._state["carbon_reduction_pct"] = reduction_pct
                self._state["poll_error"] = None
            else:
                self._state["poll_error"] = "One or more zones returned no data"

        sys.stderr.write(
            f"[CarbonPoller] {now_iso} | eastus={e_int} | westus2={w_int} | "
            f"decided={self._state.get('decided_region')}\n"
        )

    def _poll_loop(self) -> None:
        # Poll immediately on startup, then every POLL_INTERVAL_SECONDS
        while True:
            try:
                self._poll_once()
            except Exception as exc:
                sys.stderr.write(f"[CarbonPoller] Unexpected error: {exc}\n")
            time.sleep(POLL_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# Cluster Status Helper
# ---------------------------------------------------------------------------

def fetch_cluster_status() -> dict[str, Any]:
    """Query both AKS clusters for pod counts via kubectl. Non-blocking best-effort."""
    result: dict[str, Any] = {
        "primary": {"context": KUBE_CONTEXT_PRIMARY, "running": 0, "total": 0, "error": None},
        "secondary": {"context": KUBE_CONTEXT_SECONDARY, "running": 0, "total": 0, "error": None},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    for key, ctx in [("primary", KUBE_CONTEXT_PRIMARY), ("secondary", KUBE_CONTEXT_SECONDARY)]:
        try:
            proc = subprocess.run(
                [
                    "kubectl", "get", "pods",
                    "-n", BENCHMARK_NAMESPACE,
                    "--context", ctx,
                    "-o", "json",
                ],
                capture_output=True, text=True, timeout=10,
            )
            if proc.returncode != 0:
                result[key]["error"] = proc.stderr.strip()[:200]
                continue
            pods_json = json.loads(proc.stdout)
            items = pods_json.get("items", [])
            running = sum(
                1 for pod in items
                if pod.get("status", {}).get("phase") == "Running"
            )
            result[key]["running"] = running
            result[key]["total"] = len(items)
        except subprocess.TimeoutExpired:
            result[key]["error"] = "kubectl timeout"
        except (json.JSONDecodeError, Exception) as exc:
            result[key]["error"] = str(exc)[:200]

    return result


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------

class CarbonSchedulerWebHandler(SimpleHTTPRequestHandler):
    """Custom HTTP handler serving web assets and JSON telemetry endpoints."""

    carbon_poller: LiveCarbonPoller  # set on class by main()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def do_GET(self) -> None:
        if self.path.startswith("/api/"):
            self.handle_api()
        else:
            super().do_GET()

    def handle_api(self) -> None:
        path = self.path.split("?")[0]

        if path == "/api/status":
            self.send_json({
                "status": "healthy",
                "clusters": {
                    "primary": KUBE_CONTEXT_PRIMARY,
                    "secondary": KUBE_CONTEXT_SECONDARY,
                },
                "workload": "DeathStarBench Social Network",
                "version": "2.0.0",
                "live_carbon_enabled": True,
            })

        elif path == "/api/live-carbon":
            # Return the latest live carbon state from the background poller
            state = self.carbon_poller.get_state()
            self.send_json(state)

        elif path == "/api/cluster-status":
            # Real-time pod status from both AKS clusters
            status = fetch_cluster_status()
            self.send_json(status)

        elif path == "/api/summary":
            summary_file = REPO_ROOT / "artifacts" / "runs" / "summary.csv"
            if summary_file.exists():
                with open(summary_file, "r", encoding="utf-8") as f:
                    data = list(csv.DictReader(f))
                self.send_json(data)
            else:
                self.send_json([], status=HTTPStatus.NOT_FOUND)

        elif path == "/api/real-pilot":
            pilot_file = WEB_DIR / "data" / "real_pilot.json"
            if pilot_file.exists():
                self.send_json(json.loads(pilot_file.read_text(encoding="utf-8")))
            else:
                self.send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

        elif path == "/api/carbon-trace":
            trace_file = REPO_ROOT / "artifacts" / "carbon-trace.json"
            if trace_file.exists():
                self.send_json(json.loads(trace_file.read_text(encoding="utf-8")))
            else:
                self.send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

        else:
            self.send_error(HTTPStatus.NOT_FOUND, "API endpoint not found")

    def send_json(self, data: Any, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(data, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        sys.stderr.write(f"[{self.log_date_time_string()}] {format % args}\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Carbon Scheduler Showcase Web Server")
    parser.add_argument("--port", type=int, default=8000, help="Port to listen on (default: 8000)")
    parser.add_argument("--host", default="0.0.0.0", help="Host address (default: 0.0.0.0)")
    args = parser.parse_args()

    # Ensure mimetypes are properly configured
    mimetypes.add_type("application/javascript", ".js")
    mimetypes.add_type("text/css", ".css")
    mimetypes.add_type("application/json", ".json")

    # Load .env if present so env vars are available when server is run directly
    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())

    # Start the live carbon background poller
    poller = LiveCarbonPoller()
    CarbonSchedulerWebHandler.carbon_poller = poller
    poller.start()
    sys.stderr.write(
        f"[Server] LiveCarbonPoller started — polling {ZONE_EASTUS} (eastus) "
        f"and {ZONE_WESTUS2} (westus2) every {POLL_INTERVAL_SECONDS}s\n"
    )

    server_address = (args.host, args.port)
    httpd = ThreadingHTTPServer(server_address, CarbonSchedulerWebHandler)
    print(f"🌍 Carbon-Aware Cloud Showcase running at: http://localhost:{args.port}/")
    print(f"   Live carbon endpoint: http://localhost:{args.port}/api/live-carbon")
    print(f"   Cluster status:       http://localhost:{args.port}/api/cluster-status")
    print(f"   Serving web directory: {WEB_DIR}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down showcase server.")
        httpd.server_close()


if __name__ == "__main__":
    main()
