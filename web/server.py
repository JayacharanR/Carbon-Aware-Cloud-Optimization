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

The control endpoints start real cloud work, so the server only answers
requests addressed to a host name it knows (DNS rebinding), accepts POSTs only
from its own page (cross-site request forgery) and, unless
``--allow-remote-control`` is given, only from the local machine.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlencode, urlsplit
from urllib.request import Request, urlopen

if __package__:
    from .live_cycle import LiveCycleRunner
else:  # started as ``python web/server.py``: the script directory is on sys.path
    from live_cycle import LiveCycleRunner

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = REPO_ROOT / "web"
EVIDENCE_FILE = WEB_DIR / "data" / "evidence.json"

POLL_INTERVAL_SECONDS = 300          # Electricity Maps publishes hourly; 5 min is plenty
MAX_OBSERVATION_AGE_SECONDS = 7200   # the scheduler's gate v2.1 data-age limit (2 h)
CLUSTER_CACHE_SECONDS = 30

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
# Only the page's own assets are served; never Python sources, bytecode,
# dotfiles or directory listings.
STATIC_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
}
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
    # The page loads only its own scripts; inline style attributes and the
    # Google Fonts stylesheet are the two exceptions it needs.
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; "
        "connect-src 'self'; object-src 'none'; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'none'"
    ),
}


def load_dotenv(env_file: Path, environ: dict[str, str]) -> None:
    """Fill unset variables from a ``KEY=value`` file; the environment wins."""
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            environ.setdefault(key.strip().removeprefix("export ").strip(), value)


@dataclass(frozen=True)
class Settings:
    """Everything the server reads from the environment, read once in main()."""

    em_api_key: str = ""
    em_api_base: str = "https://api.electricitymaps.com/v4"
    primary_region: str = "eastus2"
    secondary_region: str = "westus2"
    zone_primary: str = "US-MIDA-PJM"
    zone_secondary: str = "US-NW-BPAT"
    kube_context_primary: str = "aks-primary-eastus2"
    kube_context_secondary: str = "aks-secondary-westus2"
    namespace: str = "benchmark"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Settings:
        base = env.get("ELECTRICITY_MAPS_API_BASE_URL", cls.em_api_base).rstrip("/")
        if urlsplit(base).scheme not in {"https", "http"}:
            raise ValueError("ELECTRICITY_MAPS_API_BASE_URL must be an http(s) URL")
        return cls(
            em_api_key=env.get("ELECTRICITY_MAPS_API_KEY", "").strip(),
            em_api_base=base,
            primary_region=env.get("AZURE_PRIMARY_REGION", cls.primary_region),
            secondary_region=env.get("AZURE_SECONDARY_REGION", cls.secondary_region),
            # The *_EASTUS/*_WESTUS2 names are the older spellings of the same settings.
            zone_primary=env.get("ELECTRICITY_MAPS_ZONE_PRIMARY", env.get("ELECTRICITY_MAPS_ZONE_EASTUS", cls.zone_primary)),
            zone_secondary=env.get("ELECTRICITY_MAPS_ZONE_SECONDARY", env.get("ELECTRICITY_MAPS_ZONE_WESTUS2", cls.zone_secondary)),
            kube_context_primary=env.get("TARGET_PRIMARY_KUBE_CONTEXT", cls.kube_context_primary),
            kube_context_secondary=env.get("TARGET_SECONDARY_KUBE_CONTEXT", cls.kube_context_secondary),
            namespace=env.get("WORKLOAD_NAMESPACE", cls.namespace),
        )


def _now() -> datetime:
    return datetime.now(UTC)


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

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = threading.Lock()
        self._zones: dict[str, dict[str, Any]] = {
            "primary": self._empty(settings.primary_region, settings.zone_primary),
            "secondary": self._empty(settings.secondary_region, settings.zone_secondary),
        }
        self._last_poll_at: str | None = None
        self._thread = threading.Thread(target=self._poll_loop, name="LiveCarbonPoller", daemon=True)

    def _empty(self, region: str, zone: str) -> dict[str, Any]:
        return {"region": region, "zone_requested": zone, "zone_returned": None, "intensity": None,
                "observed_at": None, "fetched_at": None, "is_estimated": None, "estimation_method": None,
                "emission_factor_type": None, "error": None if self.settings.em_api_key else "ELECTRICITY_MAPS_API_KEY is not set"}

    def start(self) -> None:
        self._thread.start()

    def _fetch_zone(self, zone: str) -> dict[str, Any]:
        if not self.settings.em_api_key:
            return {"error": "ELECTRICITY_MAPS_API_KEY is not set"}
        url = f"{self.settings.em_api_base}/carbon-intensity/latest?{urlencode({'zone': zone})}"
        req = Request(url, headers={"auth-token": self.settings.em_api_key, "User-Agent": "carbon-scheduler-showcase"})
        try:
            # The scheme is restricted to http(s) in Settings.from_env.
            with urlopen(req, timeout=12) as resp:  # noqa: S310
                payload = json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            return {"error": f"Electricity Maps returned HTTP {exc.code}"}
        except (URLError, TimeoutError, OSError) as exc:
            return {"error": f"Electricity Maps unreachable ({getattr(exc, 'reason', exc)})"}
        except ValueError:  # not JSON, or not UTF-8
            return {"error": "Electricity Maps returned an unreadable response"}
        intensity = payload.get("carbonIntensity") if isinstance(payload, dict) else None
        if isinstance(intensity, bool) or not isinstance(intensity, (int, float)) or not 0 <= intensity < float("inf"):
            return {"error": "Electricity Maps returned no usable intensity for this zone"}
        return {
            "zone_returned": payload.get("zone"),
            "intensity": float(intensity),
            "observed_at": payload.get("datetime"),
            "fetched_at": _now().isoformat(),
            "is_estimated": payload.get("isEstimated"),
            "estimation_method": payload.get("estimationMethod"),
            "emission_factor_type": payload.get("emissionFactorType"),
            "error": None,
        }

    def _poll_once(self) -> None:
        with ThreadPoolExecutor(max_workers=2) as pool:  # the two zones are independent requests
            fetched = pool.map(self._fetch_zone, [z["zone_requested"] for z in self._zones.values()])
            results = dict(zip(self._zones, fetched, strict=True))
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
                         f"{self.settings.primary_region}={state['primary']['intensity']} "
                         f"{self.settings.secondary_region}={state['secondary']['intensity']}"
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
        for z in zones.values():
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

def _classify_kubectl_error(stderr: str, namespace: str) -> str:
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
        return f"namespace {namespace!r} does not exist"
    return "kubectl check failed"


def _check_cluster(context: str, namespace: str) -> dict[str, Any]:
    result: dict[str, Any] = {"context": context, "namespace": namespace, "state": "unknown",
                              "running": None, "total": None, "reason": None, "detail": None}
    try:
        proc = subprocess.run(["kubectl", "get", "pods", "-n", namespace, "--context", context,
                               "--request-timeout=8s", "-o", "json"], capture_output=True, text=True, timeout=12)
    except FileNotFoundError:
        result.update(state="unknown", reason="kubectl is not installed on the web host")
        return result
    except subprocess.TimeoutExpired:
        result.update(state="unreachable", reason="API server did not answer in time")
        return result
    if proc.returncode != 0:
        result.update(state="unreachable", reason=_classify_kubectl_error(proc.stderr, namespace),
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
        result.update(state="empty", reason=f"no pods in namespace {namespace!r}")
    elif running == len(pods):
        result.update(state="ready")
    else:
        result.update(state="degraded", reason=f"{len(pods) - running} of {len(pods)} pods not running and ready")
    return result


class ClusterStatusCache:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = threading.Lock()
        # One kubectl check at a time: requests that arrive while a check runs
        # wait for it and reuse its result instead of each starting kubectl.
        self._refresh_lock = threading.Lock()
        self._value: dict[str, Any] | None = None
        self._at = 0.0

    def _cached(self, newer_than: float | None = None) -> dict[str, Any] | None:
        with self._lock:
            if self._value is None:
                return None
            if newer_than is not None:
                return self._value if self._at >= newer_than else None
            return self._value if time.monotonic() - self._at < CLUSTER_CACHE_SECONDS else None

    def get(self, fresh: bool = False) -> dict[str, Any]:
        asked_at = time.monotonic()
        if not fresh and (value := self._cached()) is not None:
            return value
        with self._refresh_lock:
            # A check that finished while this request waited is fresh enough,
            # also for fresh=True: it started after this request was made.
            value = self._cached(newer_than=asked_at) if fresh else self._cached()
            if value is not None:
                return value
            started = time.monotonic()
            contexts = [self.settings.kube_context_primary, self.settings.kube_context_secondary]
            with ThreadPoolExecutor(max_workers=2) as pool:
                primary, secondary = pool.map(lambda c: _check_cluster(c, self.settings.namespace), contexts)
            value = {"primary": primary, "secondary": secondary, "checked_at": _now().isoformat(),
                     "note": "Readiness of the two AKS stamps as seen by kubectl from the web host."}
            with self._lock:
                self._value, self._at = value, started
            return value


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

def _hostname(netloc: str) -> str | None:
    """Host name of a ``Host`` header or URL authority, without port or brackets."""
    try:
        name = urlsplit("//" + netloc).hostname
    except ValueError:
        return None
    return name.lower() if name else None


class DashboardServer(ThreadingHTTPServer):
    """HTTP server plus the shared state and the request policy of the page."""

    def __init__(self, address: tuple[str, int], settings: Settings, *, carbon_poller: LiveCarbonPoller,
                 cluster_cache: ClusterStatusCache, cycle_runner: LiveCycleRunner,
                 allowed_hosts: frozenset[str] = LOOPBACK_HOSTS, allow_remote_control: bool = False) -> None:
        self.settings = settings
        self.carbon_poller = carbon_poller
        self.cluster_cache = cluster_cache
        self.cycle_runner = cycle_runner
        self.allowed_hosts = frozenset(host.lower() for host in allowed_hosts)
        self.allow_remote_control = allow_remote_control
        super().__init__(address, CarbonSchedulerWebHandler)


class CarbonSchedulerWebHandler(SimpleHTTPRequestHandler):
    server: DashboardServer
    timeout = 30  # a client that stops sending must not hold a thread forever

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    # ------------------------------------------------------------ policy
    def _host_allowed(self) -> bool:
        """Refuse names the server was not started for (DNS rebinding)."""
        return _hostname(self.headers.get("Host", "")) in self.server.allowed_hosts

    def _same_origin(self) -> bool:
        """True unless a browser says the request comes from another site."""
        if self.headers.get("Sec-Fetch-Site", "same-origin") not in {"same-origin", "none"}:
            return False
        origin = self.headers.get("Origin")
        if origin is None:
            return True  # not a browser (curl, tests); the host check still applies
        return urlsplit(origin).netloc.lower() == self.headers.get("Host", "").lower()

    def _client_is_local(self) -> bool:
        address = ipaddress.ip_address(self.client_address[0])
        return (getattr(address, "ipv4_mapped", None) or address).is_loopback

    def _refuse(self, reason: str) -> None:
        self.send_json({"error": "forbidden", "reason": reason}, status=HTTPStatus.FORBIDDEN)

    # ---------------------------------------------------------- requests
    def do_GET(self) -> None:
        if not self._host_allowed():
            self._refuse("unknown Host header")
        elif urlsplit(self.path).path.startswith("/api/"):
            self.handle_api()
        else:
            super().do_GET()

    def do_HEAD(self) -> None:
        if not self._host_allowed():
            self._refuse("unknown Host header")
        else:
            super().do_HEAD()

    def do_POST(self) -> None:
        if not self._host_allowed():
            return self._refuse("unknown Host header")
        if not self._same_origin():
            return self._refuse("cross-site requests cannot control live cycles")
        if not (self.server.allow_remote_control or self._client_is_local()):
            return self._refuse("live cycles can only be controlled from the machine running the server")
        url = urlsplit(self.path)
        runner = self.server.cycle_runner
        if url.path == "/api/cycle/stop":
            stopped, detail = runner.stop()
            self.send_json({"stopping": stopped, "reason": detail},
                status=HTTPStatus.ACCEPTED if stopped else HTTPStatus.CONFLICT)
        elif url.path == "/api/cycle/reset":
            reset, detail = runner.reset()
            self.send_json({"reset": reset, "reason": detail},
                status=HTTPStatus.OK if reset else HTTPStatus.CONFLICT)
        elif url.path == "/api/cycle/run":
            clusters = self.server.cluster_cache.get(fresh=True)
            started, detail = runner.start(
                {clusters[k]["context"]: clusters[k]["state"] for k in ("primary", "secondary")},
                scenario=(parse_qs(url.query).get("scenario") or [None])[0])
            if started:
                self.send_json({"started": True, "run_id": detail}, status=HTTPStatus.ACCEPTED)
            else:
                self.send_json({"started": False, "reason": detail}, status=HTTPStatus.CONFLICT)
        else:
            self.send_json({"error": "API endpoint not found"}, status=HTTPStatus.NOT_FOUND)

    def send_head(self):
        """Serve a known static asset; anything else in web/ does not exist."""
        path = urlsplit(self.path).path
        if path == "/":
            path = "/index.html"
        parts = PurePosixPath(unquote(path)).parts
        content_type = STATIC_CONTENT_TYPES.get(PurePosixPath(path).suffix.lower())
        if content_type is None or any(part.startswith(".") for part in parts) \
                or not Path(self.translate_path(path)).is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return None
        self.path = path
        return super().send_head()

    def guess_type(self, path) -> str:
        return STATIC_CONTENT_TYPES.get(Path(path).suffix.lower(), "application/octet-stream")

    def end_headers(self) -> None:
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        if not urlsplit(self.path).path.startswith("/api/"):
            # Always revalidate, so an edited asset shows at once, but let the
            # browser keep its copy and get a 304 when nothing changed.
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def handle_api(self) -> None:
        path = urlsplit(self.path).path
        settings = self.server.settings
        if path == "/api/status":
            self.send_json({
                "server": "ok",
                "gate_profile": "v2.1",
                "live_carbon_configured": bool(settings.em_api_key),
                "evidence_available": EVIDENCE_FILE.exists(),
                "clusters": {"primary": settings.kube_context_primary, "secondary": settings.kube_context_secondary},
                "workload": "DeathStarBench Social Network",
                "version": "3.0.0",
            })
        elif path == "/api/live-carbon":
            self.send_json(self.server.carbon_poller.get_state())
        elif path == "/api/cluster-status":
            self.send_json(self.server.cluster_cache.get())
        elif path == "/api/cycle":
            self.send_json(self.server.cycle_runner.state())
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
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        sys.stderr.write(f"[{self.log_date_time_string()}] {format % args}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Carbon scheduler showcase web server")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1",
                        help="address to listen on (default: the local machine only)")
    parser.add_argument("--allowed-host", action="append", default=[], metavar="NAME",
                        help="extra host name or address the page may be opened under; repeatable")
    parser.add_argument("--allow-remote-control", action="store_true",
                        help="let other machines start, stop and reset live cycles (there is no login)")
    args = parser.parse_args()

    load_dotenv(REPO_ROOT / ".env", os.environ)
    settings = Settings.from_env(os.environ)
    if not settings.em_api_key:
        sys.stderr.write("[Server] ELECTRICITY_MAPS_API_KEY is not set; the live carbon panel will show 'unavailable'.\n")
    if not EVIDENCE_FILE.exists():
        sys.stderr.write("[Server] web/data/evidence.json is missing; run scripts/export_web_evidence.py.\n")

    allowed_hosts = set(LOOPBACK_HOSTS) | {name.lower() for name in args.allowed_host}
    if args.host not in {"", "0.0.0.0", "::"}:  # noqa: S104 - a wildcard is not a name a browser can ask for
        allowed_hosts.add(args.host.lower())
    local_only = args.host in LOOPBACK_HOSTS
    if not local_only:
        control = "can" if args.allow_remote_control else "cannot"
        sys.stderr.write(f"[Server] listening on {args.host}: the page is readable from the network without a login; "
                         f"other machines {control} control live cycles.\n")

    poller = LiveCarbonPoller(settings)
    httpd = DashboardServer(
        (args.host, args.port), settings, carbon_poller=poller, cluster_cache=ClusterStatusCache(settings),
        cycle_runner=LiveCycleRunner(
            clusters={settings.primary_region: settings.kube_context_primary,
                      settings.secondary_region: settings.kube_context_secondary},
            namespace=settings.namespace),
        allowed_hosts=frozenset(allowed_hosts), allow_remote_control=args.allow_remote_control)
    poller.start()
    print(f"Carbon-aware scheduler showcase: http://localhost:{args.port}/")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.server_close()


if __name__ == "__main__":
    main()
