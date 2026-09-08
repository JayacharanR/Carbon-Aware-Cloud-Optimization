"""Execution adapters for fresh benchmark sessions in independent clusters.

The Kubernetes adapter creates a new reset job and a new benchmark job in the
selected regional cluster.  It never moves an existing workload or database
between regions.
"""

from __future__ import annotations

import copy
import hashlib
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Protocol

import yaml

from .schemas import ActionProposal, DecisionMode, ExecutionReceipt


class ExecutionError(RuntimeError):
    """A requested benchmark session could not be prepared or completed."""


@dataclass(frozen=True, slots=True)
class KubernetesRegionTarget:
    """Connection and template details for one independent regional stamp."""

    region: str
    cluster_name: str
    kube_context: str
    namespace: str
    frontend_service: str
    reset_job_template: Path
    benchmark_job_template: Path
    # ``in-cluster`` uses the controller Pod's service-account credentials.
    # A remote target may instead point at a separately mounted, least-
    # privilege kubeconfig.  Keeping this explicit avoids relying on whatever
    # kubeconfig happens to be present in an image.
    kubeconfig_path: Path | None = None
    # Values that are specific to the pinned workload image/profile. Keeping
    # these outside the source template prevents an executor from inventing a
    # benchmark rate or image when a completed experiment has not supplied one.
    template_values: Mapping[str, str] = field(default_factory=dict)


class BenchmarkExecutor(Protocol):
    def execute(
        self,
        *,
        action: ActionProposal,
        mode: DecisionMode,
        run_id: str,
        cycle_id: str,
        carbon_snapshot_id: str,
        config_hash: str,
        fallback_triggered: bool,
    ) -> ExecutionReceipt: ...


class DryRunExecutor:
    """Non-experimental executor for validating controller plumbing locally.

    It deliberately returns no latency or error figures, so demo/dry-run output
    cannot be mistaken for a real workload measurement.
    """

    def __init__(self, cluster_by_region: Mapping[str, str]) -> None:
        self.cluster_by_region = dict(cluster_by_region)

    def execute(
        self,
        *,
        action: ActionProposal,
        mode: DecisionMode,
        run_id: str,
        cycle_id: str,
        carbon_snapshot_id: str,
        config_hash: str,
        fallback_triggered: bool,
    ) -> ExecutionReceipt:
        del carbon_snapshot_id, config_hash
        now = datetime.now(UTC)
        return ExecutionReceipt(
            run_id=run_id,
            cycle_id=cycle_id,
            mode=mode,
            final_action=action,
            fallback_triggered=fallback_triggered,
            cluster_name=self.cluster_by_region.get(action.target_region),
            started_at=now,
            completed_at=now,
            execution_failure_reason="dry-run: no Kubernetes reset or benchmark job was created",
        )


class KubernetesBenchmarkExecutor:
    """Idempotently execute reset then benchmark jobs through Kubernetes APIs."""

    def __init__(
        self,
        targets: Mapping[str, KubernetesRegionTarget],
        *,
        job_timeout_seconds: int = 900,
        poll_interval_seconds: float = 2.0,
        artifact_root: str | Path | None = None,
    ) -> None:
        if job_timeout_seconds <= 0:
            raise ValueError("job_timeout_seconds must be positive")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self.targets = dict(targets)
        self.job_timeout_seconds = job_timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.artifact_root = Path(artifact_root) if artifact_root is not None else None

    def execute(
        self,
        *,
        action: ActionProposal,
        mode: DecisionMode,
        run_id: str,
        cycle_id: str,
        carbon_snapshot_id: str,
        config_hash: str,
        fallback_triggered: bool,
    ) -> ExecutionReceipt:
        target = self.targets.get(action.target_region)
        if target is None:
            raise ExecutionError(f"no Kubernetes target configured for region {action.target_region}")
        started_at = datetime.now(UTC)
        artifact_paths: list[str] = []
        try:
            batch_api, core_api, api_exception = self._clients_for(target)
            self._wait_until_scheduled(action.scheduled_time)
            self._wait_for_frontend(core_api, target, api_exception)
            labels = {
                "app.kubernetes.io/managed-by": "carbon-scheduler",
                "carbon-scheduler/run-id": _label_value(run_id),
                "carbon-scheduler/cycle-id": _label_value(cycle_id),
                "carbon-scheduler/mode": mode.value,
                "carbon-scheduler/snapshot-id": _label_value(carbon_snapshot_id),
                "carbon-scheduler/config-hash": _label_value(config_hash[:12]),
            }
            reset_name = self._job_name("reset", cycle_id)
            reset_body = _render_job(
                template_path=target.reset_job_template,
                name=reset_name,
                namespace=target.namespace,
                labels=labels,
                environment={
                    **dict(target.template_values),
                    "SCHEDULER_RUN_ID": run_id,
                    "SCHEDULER_CYCLE_ID": cycle_id,
                    "CYCLE_ID": cycle_id,
                    "TARGET_REGION": target.region,
                },
            )
            self._ensure_job(batch_api, target.namespace, reset_name, reset_body, api_exception)
            try:
                self._wait_for_job(batch_api, target.namespace, reset_name)
            except Exception:
                # Preserve diagnostic output even when the reset Job fails;
                # otherwise a failed fixture reset would leave no evidence of
                # why the session was refused.
                artifact_paths.extend(
                    self._capture_job_logs(
                        core_api,
                        target.namespace,
                        reset_name,
                        cycle_id=cycle_id,
                        kind="reset",
                    )
                )
                raise
            reset_logs = self._job_logs(core_api, target.namespace, reset_name)
            artifact_paths = self._save_logs(
                cycle_id=cycle_id,
                reset_logs=reset_logs,
                benchmark_logs=None,
            )
            # The reset job may restart or temporarily drain the frontend.
            # Check readiness again before allowing the load generator to run.
            self._wait_for_frontend(core_api, target, api_exception)

            benchmark_name = self._job_name("benchmark", cycle_id)
            benchmark_body = _render_job(
                template_path=target.benchmark_job_template,
                name=benchmark_name,
                namespace=target.namespace,
                labels=labels,
                environment={
                    **dict(target.template_values),
                    "SCHEDULER_RUN_ID": run_id,
                    "SCHEDULER_CYCLE_ID": cycle_id,
                    "RUN_ID": run_id,
                    "CYCLE_ID": cycle_id,
                    "ALGORITHM_MODE": mode.value,
                    "TARGET_REGION": target.region,
                    "CARBON_SNAPSHOT_ID": carbon_snapshot_id,
                    "CONFIG_HASH": _label_value(config_hash[:12]),
                    "WORKLOAD_PROFILE": action.workload_profile,
                    "FRONTEND_SERVICE": target.frontend_service,
                },
            )
            self._ensure_job(batch_api, target.namespace, benchmark_name, benchmark_body, api_exception)
            try:
                self._wait_for_job(batch_api, target.namespace, benchmark_name)
            except Exception:
                artifact_paths.extend(
                    self._capture_job_logs(
                        core_api,
                        target.namespace,
                        benchmark_name,
                        cycle_id=cycle_id,
                        kind="benchmark",
                    )
                )
                raise
            logs = self._job_logs(core_api, target.namespace, benchmark_name)
            artifact_paths.extend(
                self._save_logs(
                    cycle_id=cycle_id,
                    reset_logs=None,
                    benchmark_logs=logs,
                )
            )
            p95_latency, error_rate = parse_benchmark_output(logs)
            completed_at = datetime.now(UTC)
            return ExecutionReceipt(
                run_id=run_id,
                cycle_id=cycle_id,
                mode=mode,
                final_action=action,
                fallback_triggered=fallback_triggered,
                cluster_name=target.cluster_name,
                job_name=benchmark_name,
                started_at=started_at,
                completed_at=completed_at,
                benchmark_artifact_paths=artifact_paths,
                actual_p95_latency_ms=p95_latency,
                actual_error_rate=error_rate,
                slo_passed=None,
            )
        except Exception as error:
            return ExecutionReceipt(
                run_id=run_id,
                cycle_id=cycle_id,
                mode=mode,
                final_action=action,
                fallback_triggered=fallback_triggered,
                cluster_name=target.cluster_name,
                started_at=started_at,
                completed_at=datetime.now(UTC),
                benchmark_artifact_paths=artifact_paths,
                execution_failure_reason=str(error),
            )

    @staticmethod
    def _clients_for(target: KubernetesRegionTarget) -> tuple[Any, Any, type[Exception]]:
        try:
            from kubernetes import client, config
            from kubernetes.client.exceptions import ApiException
        except ImportError as error:  # pragma: no cover - cloud-only optional dependency
            raise ExecutionError("kubernetes package is required for real benchmark execution") from error
        try:
            if target.kube_context.strip().lower() in {"in-cluster", "in_cluster", "incluster"}:
                config.load_incluster_config()
            else:
                config.load_kube_config(
                    context=target.kube_context,
                    config_file=str(target.kubeconfig_path) if target.kubeconfig_path else None,
                )
        except Exception as error:
            raise ExecutionError(
                f"could not load Kubernetes credentials for context {target.kube_context} "
                f"in {target.region}"
            ) from error
        api_client = client.ApiClient()
        return client.BatchV1Api(api_client), client.CoreV1Api(api_client), ApiException

    @staticmethod
    def _wait_until_scheduled(scheduled_time: datetime | None) -> None:
        """Honor a delayed action without delaying already-due replay actions."""

        if scheduled_time is None:
            return
        if scheduled_time.tzinfo is None or scheduled_time.utcoffset() is None:
            raise ExecutionError("scheduled_time must include a timezone")
        target_epoch = scheduled_time.astimezone(UTC).timestamp()
        while True:
            remaining = target_epoch - time.time()
            if remaining <= 0:
                return
            # Wake periodically so a cancelled/restarted controller is not
            # stuck in one uninterruptible sleep.  A future action may be
            # intentionally hours away; the scheduling window, not the Job
            # timeout, determines when it becomes runnable.
            time.sleep(min(remaining, 30.0))

    def _wait_for_frontend(self, core_api: Any, target: KubernetesRegionTarget, api_exception: type[Exception]) -> None:
        deadline = time.monotonic() + self.job_timeout_seconds
        while True:
            try:
                endpoints = core_api.read_namespaced_endpoints(
                    name=target.frontend_service,
                    namespace=target.namespace,
                )
                subsets = getattr(endpoints, "subsets", None) or []
                if any(getattr(subset, "addresses", None) for subset in subsets):
                    return
            except api_exception as error:
                if getattr(error, "status", None) not in {404}:
                    raise ExecutionError(
                        f"unable to inspect frontend service {target.frontend_service}: {error}"
                    ) from error
            if time.monotonic() >= deadline:
                raise ExecutionError(
                    f"frontend service {target.frontend_service} has no ready endpoints before timeout"
                )
            time.sleep(self.poll_interval_seconds)

    @staticmethod
    def _ensure_job(
        batch_api: Any,
        namespace: str,
        name: str,
        body: dict[str, Any],
        api_exception: type[Exception],
    ) -> None:
        try:
            batch_api.read_namespaced_job(name=name, namespace=namespace)
            return
        except api_exception as error:
            if getattr(error, "status", None) != 404:
                raise ExecutionError(f"unable to inspect Job {name}: {error}") from error
        try:
            batch_api.create_namespaced_job(namespace=namespace, body=body)
        except api_exception as error:
            raise ExecutionError(f"unable to create Job {name}: {error}") from error

    def _wait_for_job(self, batch_api: Any, namespace: str, name: str) -> None:
        deadline = time.monotonic() + self.job_timeout_seconds
        while True:
            job = batch_api.read_namespaced_job(name=name, namespace=namespace)
            status = getattr(job, "status", None)
            if getattr(status, "succeeded", 0):
                return
            if getattr(status, "failed", 0):
                raise ExecutionError(f"Job {name} failed")
            if time.monotonic() >= deadline:
                raise ExecutionError(f"Job {name} did not finish before timeout")
            time.sleep(self.poll_interval_seconds)

    @staticmethod
    def _job_logs(core_api: Any, namespace: str, job_name: str) -> str:
        pods = core_api.list_namespaced_pod(
            namespace=namespace,
            label_selector=f"job-name={job_name}",
        ).items
        fragments: list[str] = []
        for pod in pods:
            pod_name = getattr(getattr(pod, "metadata", None), "name", None)
            if not pod_name:
                continue
            try:
                fragments.append(core_api.read_namespaced_pod_log(name=pod_name, namespace=namespace))
            except Exception as error:  # logs are diagnostic, not a reason to hide completion
                fragments.append(f"[unable to read logs for {pod_name}: {error}]")
        return "\n".join(fragments)

    def _save_logs(
        self,
        *,
        cycle_id: str,
        reset_logs: str | None,
        benchmark_logs: str | None,
    ) -> list[str]:
        """Persist raw Job output without replacing an existing artifact."""

        if self.artifact_root is None:
            return []
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        paths: list[str] = []
        for suffix, content in (("reset", reset_logs), ("benchmark", benchmark_logs)):
            if content is None:
                continue
            filename = f"{_safe_file_stem(cycle_id)}-{suffix}.log"
            destination = self.artifact_root / filename
            if not destination.exists():
                destination.write_text(content, encoding="utf-8")
            try:
                paths.append(destination.relative_to(self.artifact_root.parent).as_posix())
            except ValueError:
                # A caller may intentionally provide an artifact root outside
                # the run directory; preserve that explicit path in that case.
                paths.append(str(destination))
        return paths

    def _capture_job_logs(
        self,
        core_api: Any,
        namespace: str,
        job_name: str,
        *,
        cycle_id: str,
        kind: str,
    ) -> list[str]:
        """Best-effort log capture used while constructing a failure receipt."""

        try:
            logs = self._job_logs(core_api, namespace, job_name)
        except Exception as error:  # log collection must not hide the Job failure
            logs = f"[unable to read logs for failed {kind} Job {job_name}: {error}]"
        if kind == "reset":
            return self._save_logs(cycle_id=cycle_id, reset_logs=logs, benchmark_logs=None)
        return self._save_logs(cycle_id=cycle_id, reset_logs=None, benchmark_logs=logs)

    @staticmethod
    def _job_name(prefix: str, cycle_id: str) -> str:
        suffix = re.sub(r"[^a-z0-9-]+", "-", cycle_id.lower()).strip("-")
        suffix = suffix or "cycle"
        # Preserve uniqueness even when a long run ID would otherwise be
        # truncated before its cycle number.  The controller always supplies
        # CYCLE_ID separately to the template, so the hash is only a Job-name
        # identity component.
        digest = hashlib.sha256(cycle_id.encode("utf-8")).hexdigest()[:8]
        available = max(1, 63 - len(prefix) - len(digest) - 2)
        return f"{prefix}-{suffix[:available]}-{digest}".rstrip("-")


def parse_benchmark_output(output: str) -> tuple[float | None, float | None]:
    """Extract only explicitly reported p95/error figures from benchmark logs.

    `wrk` commonly reports p99 but not p95.  The parser extracts p95 from either
    the standard percentile summary or the wrk2 HdrHistogram detailed spectrum table.
    """

    p95_latency = _find_latency_ms(output, r"(?:p95|95%)\s*(?:latency)?\s*[:=]?\s*([0-9.]+)\s*(us|µs|ms|s)\b")
    if p95_latency is None:
        match = re.search(r"^\s*([0-9.]+)\s+0\.950000\b", output, flags=re.MULTILINE)
        if match:
            p95_latency = float(match.group(1))

    total_requests = _find_number(output, r"\b([0-9][0-9,]*)\s+requests\b")
    non_success = _find_number(
        output,
        r"\bNon-2xx\s+or\s+3xx\s+responses:\s*([0-9][0-9,]*)\b",
    )
    socket_errors_match = re.search(
        r"Socket errors:\s*connect\s*(\d+),\s*read\s*(\d+),\s*write\s*(\d+),\s*timeout\s*(\d+)",
        output,
    )
    socket_errors = 0
    if socket_errors_match:
        socket_errors = sum(int(socket_errors_match.group(i)) for i in range(1, 5))

    error_rate = None
    if total_requests is not None and total_requests > 0:
        if non_success is not None or socket_errors_match is not None:
            error_count = (non_success or 0) + socket_errors
            error_rate = error_count / total_requests
    return p95_latency, error_rate


def _find_latency_ms(output: str, pattern: str) -> float | None:
    match = re.search(pattern, output, flags=re.IGNORECASE)
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2).lower()
    factor = {"us": 0.001, "µs": 0.001, "ms": 1.0, "s": 1000.0}[unit]
    return value * factor


def _find_number(output: str, pattern: str) -> int | None:
    match = re.search(pattern, output, flags=re.IGNORECASE)
    return int(match.group(1).replace(",", "")) if match else None


def _render_job(
    *,
    template_path: Path,
    name: str,
    namespace: str,
    labels: Mapping[str, str],
    environment: Mapping[str, str],
) -> dict[str, Any]:
    """Load a Job template and add deterministic identity/command context."""

    try:
        raw = yaml.safe_load(template_path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ExecutionError(f"cannot read Job template {template_path}") from error
    except yaml.YAMLError as error:
        raise ExecutionError(f"invalid Job template YAML {template_path}") from error
    if not isinstance(raw, dict) or raw.get("kind") != "Job":
        raise ExecutionError(f"Job template {template_path} must contain one Kubernetes Job")
    body = copy.deepcopy(raw)
    # Templates are intentionally plain YAML so they can be reviewed without
    # Helm. Substitute only values supplied by the controller. Unknown tokens
    # are rejected instead of reaching a cluster literally.
    substitutions = {
        "CYCLE_ID": environment.get("CYCLE_ID", cycle_id_from_name(name)),
        "RUN_ID": environment.get("RUN_ID", environment.get("SCHEDULER_RUN_ID", "")),
        "ALGORITHM_MODE": environment.get("ALGORITHM_MODE", labels.get("carbon-scheduler/mode", "")),
        "TARGET_REGION": environment.get("TARGET_REGION", labels.get("carbon-scheduler/region", "")),
        "CARBON_SNAPSHOT_ID": _label_value(environment.get("CARBON_SNAPSHOT_ID", labels.get("carbon-scheduler/snapshot-id", ""))),
        "CONFIG_HASH": _label_value(environment.get("CONFIG_HASH", labels.get("carbon-scheduler/config-hash", ""))),
        "FRONTEND_HOST": environment.get("FRONTEND_HOST", ""),
        "FRONTEND_PORT": environment.get("FRONTEND_PORT", "8080"),
        "TARGET_URL": environment.get("TARGET_URL", ""),
        "WORKLOAD_PROFILE": environment.get("WORKLOAD_PROFILE", ""),
        "DSB_TOOLS_IMAGE": environment.get("DSB_TOOLS_IMAGE", ""),
        "GRAPH_DATASET": environment.get("GRAPH_DATASET", ""),
        "SEED_LIMIT": environment.get("SEED_LIMIT", "50"),
        "ACTIVE_DEADLINE_SECONDS": environment.get("ACTIVE_DEADLINE_SECONDS", ""),
        "WRK_THREADS": environment.get("WRK_THREADS", ""),
        "WRK_CONNECTIONS": environment.get("WRK_CONNECTIONS", ""),
        "WRK_DURATION": environment.get("WRK_DURATION", ""),
        "REQUESTS_PER_SECOND": environment.get("REQUESTS_PER_SECOND", ""),
        "LOADGEN_CPU_REQUEST": environment.get("LOADGEN_CPU_REQUEST", ""),
        "LOADGEN_MEMORY_REQUEST": environment.get("LOADGEN_MEMORY_REQUEST", ""),
    }
    body = _substitute_template(body, substitutions, template_path)
    metadata = body.setdefault("metadata", {})
    metadata["name"] = name
    metadata.pop("generateName", None)
    metadata["namespace"] = namespace
    metadata["labels"] = {**dict(metadata.get("labels") or {}), **dict(labels)}
    pod_metadata = body.setdefault("spec", {}).setdefault("template", {}).setdefault("metadata", {})
    pod_metadata["labels"] = {**dict(pod_metadata.get("labels") or {}), **dict(labels)}
    spec = body["spec"]["template"].setdefault("spec", {})
    containers = spec.get("containers")
    if not isinstance(containers, list) or not containers:
        raise ExecutionError(f"Job template {template_path} contains no pod containers")
    for container in containers:
        if not isinstance(container, dict):
            raise ExecutionError(f"Job template {template_path} contains an invalid container")
        existing = {
            item.get("name"): item
            for item in container.get("env", [])
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
        for key, value in environment.items():
            existing[key] = {"name": key, "value": value}
        container["env"] = [existing[key] for key in sorted(existing)]
    return body


def cycle_id_from_name(name: str) -> str:
    """Recover the cycle suffix used by the executor's deterministic names."""

    return name.split("-", 1)[1] if "-" in name else name


def _substitute_template(value: Any, substitutions: Mapping[str, str], template_path: Path) -> Any:
    if isinstance(value, dict):
        return {
            key: _substitute_template(child, substitutions, template_path)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_substitute_template(child, substitutions, template_path) for child in value]
    if not isinstance(value, str):
        return value
    # Kubernetes expects this field as an integer, while all controller
    # environment/template values intentionally arrive as strings.  Convert
    # only the one numeric field used by the reviewed Job templates; every
    # other value remains a string and is substituted literally.
    if value == "${ACTIVE_DEADLINE_SECONDS}":
        replacement = substitutions.get("ACTIVE_DEADLINE_SECONDS", "")
        if not replacement:
            raise ExecutionError(
                f"Job template {template_path} requires a nonempty value for ACTIVE_DEADLINE_SECONDS"
            )
        try:
            numeric = int(replacement)
        except ValueError as error:
            raise ExecutionError(
                f"ACTIVE_DEADLINE_SECONDS must be an integer for Job template {template_path}"
            ) from error
        if numeric <= 0:
            raise ExecutionError(
                f"ACTIVE_DEADLINE_SECONDS must be positive for Job template {template_path}"
            )
        return numeric
    rendered = value
    for key, replacement in substitutions.items():
        if replacement == "" and "${" + key + "}" in rendered:
            raise ExecutionError(
                f"Job template {template_path} requires a nonempty value for {key}"
            )
        rendered = rendered.replace("${" + key + "}", replacement)
    if "${" in rendered:
        raise ExecutionError(
            f"Job template {template_path} contains an unresolved placeholder: {rendered}"
        )
    return rendered


def _label_value(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-._")
    return (normalized or "unknown")[:63]


def _safe_file_stem(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-._")
    return (normalized or "cycle")[:120]
