"""Render private project configuration from one operator-managed ``.env``.

The repository intentionally keeps experiment drafts incomplete because real
regions, pilot measurements, and endpoint names cannot be guessed.  This
command is the bridge from those drafts to the files consumed by the runner:

* ``--phase infra`` writes the ignored Bicep parameter files;
* ``--phase experiment`` writes ``config/experiment.yaml``,
  ``config/targets.yaml``, and ``config/latency_catalog.yaml``;
* ``--phase all`` performs both sets of writes.

Secrets are read only from the process environment/.env and are never written
to generated YAML, Bicep, run manifests, or stdout.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Any, Mapping

import yaml

try:  # direct script invocation
    from _common import REPO_ROOT, load_project_environment, resolve_path  # type: ignore
except ImportError:  # package invocation
    from scripts._common import REPO_ROOT, load_project_environment, resolve_path  # type: ignore

from carbon_scheduler.settings import ConfigurationError, load_experiment_config  # noqa: E402


_PLACEHOLDER_RE = re.compile(
    r"(?:REPLACE_ME|CHANGE_ME|CHANGEME|YOUR_[A-Z0-9_]+|<[^>]+>)",
    re.IGNORECASE,
)
_REGION_ROLES = ("PRIMARY", "SECONDARY")
_COMMON_TEMPLATE_KEYS = {
    "DSB_TOOLS_IMAGE": "DSB_TOOLS_IMAGE",
    "GRAPH_DATASET": "DSB_GRAPH_DATASET",
    "ACTIVE_DEADLINE_SECONDS": "DSB_ACTIVE_DEADLINE_SECONDS",
    "WRK_THREADS": "DSB_WRK_THREADS",
    "WRK_CONNECTIONS": "DSB_WRK_CONNECTIONS",
    "WRK_DURATION": "DSB_WRK_DURATION",
    "REQUESTS_PER_SECOND": "DSB_REQUESTS_PER_SECOND",
    "LOADGEN_CPU_REQUEST": "DSB_LOADGEN_CPU_REQUEST",
    "LOADGEN_MEMORY_REQUEST": "DSB_LOADGEN_MEMORY_REQUEST",
}
_REGION_TEMPLATE_OVERRIDES = {
    "DSB_TOOLS_IMAGE": "DSB_TOOLS_IMAGE",
    "GRAPH_DATASET": "GRAPH_DATASET",
    "ACTIVE_DEADLINE_SECONDS": "ACTIVE_DEADLINE_SECONDS",
    "WRK_THREADS": "WRK_THREADS",
    "WRK_CONNECTIONS": "WRK_CONNECTIONS",
    "WRK_DURATION": "WRK_DURATION",
    "REQUESTS_PER_SECOND": "REQUESTS_PER_SECOND",
    "LOADGEN_CPU_REQUEST": "LOADGEN_CPU_REQUEST",
    "LOADGEN_MEMORY_REQUEST": "LOADGEN_MEMORY_REQUEST",
}


class BootstrapError(ValueError):
    """Raised when .env cannot safely produce a runnable configuration."""


def _is_present(value: str | None) -> bool:
    if value is None:
        return False
    stripped = value.strip()
    return bool(stripped) and not _PLACEHOLDER_RE.search(stripped)


def _value(env: Mapping[str, str], name: str, *, default: str | None = None) -> str | None:
    raw = env.get(name)
    if _is_present(raw):
        return str(raw).strip()
    if _is_present(default):
        return str(default).strip()
    return None


def _required(
    env: Mapping[str, str],
    names: list[str],
    *,
    missing: list[str],
) -> dict[str, str]:
    values: dict[str, str] = {}
    for name in names:
        item = _value(env, name)
        if item is None:
            missing.append(name)
        else:
            values[name] = item
    return values


def _integer(env: Mapping[str, str], name: str, *, default: int | None = None) -> int:
    raw = _value(env, name, default=None if default is None else str(default))
    if raw is None:
        raise BootstrapError(f"{name} is required")
    try:
        return int(raw)
    except ValueError as error:
        raise BootstrapError(f"{name} must be an integer, got {raw!r}") from error


def _number(env: Mapping[str, str], name: str, *, default: float | None = None) -> float:
    raw = _value(env, name, default=None if default is None else str(default))
    if raw is None:
        raise BootstrapError(f"{name} is required")
    try:
        return float(raw)
    except ValueError as error:
        raise BootstrapError(f"{name} must be numeric, got {raw!r}") from error


def _boolean(env: Mapping[str, str], name: str, *, default: bool = True) -> bool:
    raw = _value(env, name, default="true" if default else "false")
    if raw is None:
        return default
    normalized = raw.lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise BootstrapError(f"{name} must be true/false, got {raw!r}")


def _csv(env: Mapping[str, str], name: str, *, default: list[str] | None = None) -> list[str]:
    raw = _value(env, name)
    if raw is None:
        return list(default or [])
    values = [part.strip() for part in raw.split(",") if part.strip()]
    if not values:
        raise BootstrapError(f"{name} must contain at least one comma-separated value")
    return values


def _load_environment(path: str | Path) -> dict[str, str]:
    dotenv_path = resolve_path(path)
    if not dotenv_path.exists():
        raise BootstrapError(
            f"environment file does not exist: {dotenv_path}. Copy .env.example to .env first."
        )
    # The shared loader keeps explicit shell variables authoritative.
    load_project_environment(dotenv_path)
    return {key: value for key, value in os.environ.items() if isinstance(value, str)}


def _ssh_public_key(env: Mapping[str, str]) -> str:
    inline = _value(env, "AZURE_SSH_PUBLIC_KEY")
    if inline:
        return inline
    path = _value(env, "AZURE_SSH_PUBLIC_KEY_PATH")
    if not path:
        raise BootstrapError("set AZURE_SSH_PUBLIC_KEY or AZURE_SSH_PUBLIC_KEY_PATH")
    key_path = resolve_path(path)
    try:
        value = key_path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise BootstrapError(f"cannot read AZURE_SSH_PUBLIC_KEY_PATH: {key_path}") from error
    if not value:
        raise BootstrapError(f"SSH public key file is empty: {key_path}")
    return value


def _bicep_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _write(path: Path, content: str, *, force: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not force:
        raise BootstrapError(
            f"refusing to overwrite existing file {path}; pass --force after reviewing the values"
        )
    path.write_text(content, encoding="utf-8", newline="\n")
    print(f"wrote {path}")


def _infra_missing(env: Mapping[str, str]) -> list[str]:
    missing: list[str] = []
    _required(
        env,
        [
            "AZURE_SUBSCRIPTION_ID",
            "AZURE_RESOURCE_GROUP",
            "AZURE_PRIMARY_REGION",
            "AZURE_SECONDARY_REGION",
            "AZURE_PRIMARY_CLUSTER",
            "AZURE_SECONDARY_CLUSTER",
            "AZURE_PRIMARY_DNS_PREFIX",
            "AZURE_SECONDARY_DNS_PREFIX",
            "AZURE_DEPLOYMENT_LOCATION",
            "AZURE_KUBERNETES_VERSION",
            "AZURE_BUDGET_NAME",
            "AZURE_BUDGET_MONTHLY_AMOUNT",
            "AZURE_BUDGET_CONTACT_EMAILS",
            "AZURE_BUDGET_START_DATE",
            "AZURE_BUDGET_END_DATE",
        ],
        missing=missing,
    )
    if not _value(env, "AZURE_SSH_PUBLIC_KEY") and not _value(env, "AZURE_SSH_PUBLIC_KEY_PATH"):
        missing.append("AZURE_SSH_PUBLIC_KEY or AZURE_SSH_PUBLIC_KEY_PATH")
    return missing


def _experiment_missing(env: Mapping[str, str]) -> list[str]:
    missing: list[str] = []
    _required(
        env,
        [
            "AZURE_PRIMARY_REGION",
            "AZURE_SECONDARY_REGION",
            "AZURE_PRIMARY_CLUSTER",
            "AZURE_SECONDARY_CLUSTER",
            "CARBON_PRIMARY_PROVIDER_REGION",
            "CARBON_SECONDARY_PROVIDER_REGION",
            "CARBON_REPLAY_TRACE_PATH",
            "WORKLOAD_FRONTEND_SERVICE",
            "WORKLOAD_BENCHMARK_SCRIPT",
            "WORKLOAD_PROFILE_ID",
            "WORKLOAD_EARLIEST_START",
            "WORKLOAD_DEADLINE",
            "SLO_PROFILE_P95_LIMIT_MS",
            "SLO_PROFILE_ERROR_RATE_LIMIT",
            "TRUST_THRESHOLD",
            "TRUST_WEIGHT_RAGAS_FAITHFULNESS",
            "TRUST_WEIGHT_RAGAS_CONTEXT_PRECISION",
            "TRUST_WEIGHT_NEMO_RAILS",
            "TRUST_WEIGHT_DATA_FRESHNESS",
            "TRUST_WEIGHT_EXECUTION_FEASIBILITY",
            "MILP_LATENCY_CATALOG_PATH",
            "EXPERIMENT_STATIC_REFERENCE_REGION",
            "EXPERIMENT_RANDOM_SEED",
            "DEATHSTARBENCH_SOURCE",
            "DEATHSTARBENCH_COMMIT",
            "DSB_TOOLS_IMAGE",
            "DSB_GRAPH_DATASET",
            "TARGET_PRIMARY_KUBE_CONTEXT",
            "TARGET_PRIMARY_FRONTEND_SERVICE",
            "TARGET_PRIMARY_FRONTEND_HOST",
            "TARGET_PRIMARY_FRONTEND_PORT",
            "TARGET_PRIMARY_TARGET_URL",
            "TARGET_SECONDARY_KUBE_CONTEXT",
            "TARGET_SECONDARY_FRONTEND_SERVICE",
            "TARGET_SECONDARY_FRONTEND_HOST",
            "TARGET_SECONDARY_FRONTEND_PORT",
            "TARGET_SECONDARY_TARGET_URL",
            "LATENCY_PRIMARY_P95_MS",
            "LATENCY_SECONDARY_P95_MS",
        ],
        missing=missing,
    )
    common_values = list(_COMMON_TEMPLATE_KEYS.values())
    _required(env, common_values, missing=missing)
    return missing


def _render_infrastructure(env: Mapping[str, str], *, force: bool) -> None:
    ssh_key = _ssh_public_key(env)
    main_lines = [
        "using './main.bicep'",
        "",
        f"param resourceGroupName = {_bicep_string(_value(env, 'AZURE_RESOURCE_GROUP') or '')}",
        f"param primaryLocation = {_bicep_string(_value(env, 'AZURE_PRIMARY_REGION') or '')}",
        f"param secondaryLocation = {_bicep_string(_value(env, 'AZURE_SECONDARY_REGION') or '')}",
        f"param primaryClusterName = {_bicep_string(_value(env, 'AZURE_PRIMARY_CLUSTER') or '')}",
        f"param secondaryClusterName = {_bicep_string(_value(env, 'AZURE_SECONDARY_CLUSTER') or '')}",
        f"param primaryDnsPrefix = {_bicep_string(_value(env, 'AZURE_PRIMARY_DNS_PREFIX') or '')}",
        f"param secondaryDnsPrefix = {_bicep_string(_value(env, 'AZURE_SECONDARY_DNS_PREFIX') or '')}",
        f"param kubernetesVersion = {_bicep_string(_value(env, 'AZURE_KUBERNETES_VERSION') or '')}",
        f"param sshPublicKey = {_bicep_string(ssh_key)}",
        f"param adminUsername = {_bicep_string(_value(env, 'AZURE_ADMIN_USERNAME', default='azureuser') or 'azureuser')}",
        f"param nodeVmSize = {_bicep_string(_value(env, 'AZURE_NODE_VM_SIZE', default='Standard_B2s') or 'Standard_B2s')}",
        f"param nodeCount = {_integer(env, 'AZURE_NODE_COUNT', default=2)}",
        f"param maxPods = {_integer(env, 'AZURE_MAX_PODS', default=30)}",
        "",
    ]
    _write(REPO_ROOT / "infra" / "bicep" / "main.bicepparam", "\n".join(main_lines), force=force)

    emails = _csv(env, "AZURE_BUDGET_CONTACT_EMAILS")
    email_lines = ",\n".join(f"  {_bicep_string(item)}" for item in emails)
    budget_lines = [
        "using './budget.bicep'",
        "",
        f"param budgetName = {_bicep_string(_value(env, 'AZURE_BUDGET_NAME') or '')}",
        f"param monthlyAmount = {_integer(env, 'AZURE_BUDGET_MONTHLY_AMOUNT')}",
        "param contactEmails = [",
        email_lines,
        "]",
        f"param startDate = {_bicep_string(_value(env, 'AZURE_BUDGET_START_DATE') or '')}",
        f"param endDate = {_bicep_string(_value(env, 'AZURE_BUDGET_END_DATE') or '')}",
        "",
    ]
    _write(REPO_ROOT / "infra" / "bicep" / "budget.bicepparam", "\n".join(budget_lines), force=force)


def _region_value(env: Mapping[str, str], role: str, suffix: str, common_name: str) -> str:
    specific = _value(env, f"TARGET_{role}_{suffix}")
    common = _value(env, common_name)
    if specific:
        return specific
    if common:
        return common
    raise BootstrapError(f"TARGET_{role}_{suffix} or {common_name} is required")


def _render_experiment(env: Mapping[str, str], *, force: bool) -> None:
    primary_region = _value(env, "AZURE_PRIMARY_REGION") or ""
    secondary_region = _value(env, "AZURE_SECONDARY_REGION") or ""
    allowed_regions = _csv(env, "MILP_ALLOWED_REGIONS", default=[primary_region, secondary_region])
    modes = _csv(env, "EXPERIMENT_MODES", default=["llm_only", "milp_only", "hybrid"])

    experiment = {
        "azure": {
            "resource_group": _value(env, "AZURE_RESOURCE_GROUP") or "",
            "primary_region": primary_region,
            "secondary_region": secondary_region,
            "primary_cluster": _value(env, "AZURE_PRIMARY_CLUSTER") or "",
            "secondary_cluster": _value(env, "AZURE_SECONDARY_CLUSTER") or "",
        },
        "carbon": {
            "api_base_url": _value(
                env, "ELECTRICITY_MAPS_API_BASE_URL", default="https://api.electricitymaps.com/v4"
            ),
            "provider": _value(env, "CARBON_PROVIDER", default="azure"),
            "primary_provider_region": _value(env, "CARBON_PRIMARY_PROVIDER_REGION") or "",
            "secondary_provider_region": _value(env, "CARBON_SECONDARY_PROVIDER_REGION") or "",
            "forecast_horizon_hours": _integer(env, "CARBON_FORECAST_HORIZON_HOURS", default=24),
            "cache_directory": _value(env, "CARBON_CACHE_DIRECTORY", default="data/carbon_cache"),
            "cache_max_age_seconds": _integer(env, "CARBON_CACHE_MAX_AGE_SECONDS", default=86400),
            "replay_trace_path": _value(env, "CARBON_REPLAY_TRACE_PATH"),
        },
        "workload": {
            "namespace": _value(env, "WORKLOAD_NAMESPACE", default="benchmark"),
            "frontend_service": _value(env, "WORKLOAD_FRONTEND_SERVICE") or "",
            "benchmark_script": _value(env, "WORKLOAD_BENCHMARK_SCRIPT", default="compose-post.lua"),
            "profile_id": _value(env, "WORKLOAD_PROFILE_ID") or "",
            "earliest_start": _value(env, "WORKLOAD_EARLIEST_START") or "",
            "deadline": _value(env, "WORKLOAD_DEADLINE") or "",
            "reset_before_session": _boolean(env, "WORKLOAD_RESET_BEFORE_SESSION", default=True),
        },
        "slo": {
            "profile_p95_limit_ms": _number(env, "SLO_PROFILE_P95_LIMIT_MS"),
            "profile_error_rate_limit": _number(env, "SLO_PROFILE_ERROR_RATE_LIMIT"),
        },
        "trust": {
            "threshold": _number(env, "TRUST_THRESHOLD"),
            "weights": {
                "ragas_faithfulness": _number(env, "TRUST_WEIGHT_RAGAS_FAITHFULNESS"),
                "ragas_context_precision": _number(env, "TRUST_WEIGHT_RAGAS_CONTEXT_PRECISION"),
                "nemo_rails": _number(env, "TRUST_WEIGHT_NEMO_RAILS"),
                "data_freshness": _number(env, "TRUST_WEIGHT_DATA_FRESHNESS"),
                "execution_feasibility": _number(env, "TRUST_WEIGHT_EXECUTION_FEASIBILITY"),
            },
        },
        "milp": {
            "slot_minutes": _integer(env, "MILP_SLOT_MINUTES", default=5),
            "allowed_regions": allowed_regions,
            "latency_catalog_path": _value(
                env, "MILP_LATENCY_CATALOG_PATH", default="config/latency_catalog.yaml"
            ),
        },
        "ollama": {
            "base_url": _value(env, "OLLAMA_BASE_URL") or "",
            "model": _value(env, "OLLAMA_MODEL") or "",
            "request_timeout_seconds": _integer(env, "OLLAMA_REQUEST_TIMEOUT_SECONDS", default=120),
            "retry_count": _integer(env, "OLLAMA_RETRY_COUNT", default=2),
        },
        "experiments": {
            "modes": modes,
            "static_reference_region": _value(env, "EXPERIMENT_STATIC_REFERENCE_REGION") or "",
            "random_seed": _integer(env, "EXPERIMENT_RANDOM_SEED", default=1),
        },
    }
    experiment_path = REPO_ROOT / "config" / "experiment.yaml"
    _write(experiment_path, yaml.safe_dump(experiment, sort_keys=False), force=force)

    targets: dict[str, Any] = {"regions": {}}
    for role, region, cluster_env in (
        ("PRIMARY", primary_region, "AZURE_PRIMARY_CLUSTER"),
        ("SECONDARY", secondary_region, "AZURE_SECONDARY_CLUSTER"),
    ):
        target = {
            "cluster_name": _value(
                env, f"TARGET_{role}_CLUSTER_NAME", default=_value(env, cluster_env)
            ),
            "kube_context": _value(env, f"TARGET_{role}_KUBE_CONTEXT") or "",
            "kubeconfig_path": _value(env, f"TARGET_{role}_KUBECONFIG_PATH") or "",
            "namespace": _value(env, "WORKLOAD_NAMESPACE", default="benchmark"),
            "frontend_service": _value(env, f"TARGET_{role}_FRONTEND_SERVICE") or "",
            "reset_job_template": "deploy/deathstarbench/reset-job.yaml",
            "benchmark_job_template": "deploy/deathstarbench/benchmark-job.yaml",
            "template_values": {},
        }
        template_values: dict[str, str] = {}
        for template_key, common_name in _COMMON_TEMPLATE_KEYS.items():
            override_suffix = _REGION_TEMPLATE_OVERRIDES[template_key]
            template_values[template_key] = _region_value(
                env,
                role,
                override_suffix,
                common_name,
            )
        template_values.update(
            {
                "FRONTEND_HOST": _value(env, f"TARGET_{role}_FRONTEND_HOST") or "",
                "FRONTEND_PORT": _value(env, f"TARGET_{role}_FRONTEND_PORT") or "",
                "TARGET_URL": _value(env, f"TARGET_{role}_TARGET_URL") or "",
            }
        )
        target["template_values"] = template_values
        targets["regions"][region] = target
    target_path = REPO_ROOT / "config" / "targets.yaml"
    _write(target_path, yaml.safe_dump(targets, sort_keys=False), force=force)

    profile = _value(env, "WORKLOAD_PROFILE_ID") or ""
    latency = {
        profile: {
            primary_region: _number(env, "LATENCY_PRIMARY_P95_MS"),
            secondary_region: _number(env, "LATENCY_SECONDARY_P95_MS"),
        }
    }
    latency_path = resolve_path(_value(env, "MILP_LATENCY_CATALOG_PATH", default="config/latency_catalog.yaml"))
    _write(latency_path, yaml.safe_dump(latency, sort_keys=False), force=force)

    try:
        load_experiment_config(experiment_path)
    except ConfigurationError as error:
        raise BootstrapError(f"generated experiment.yaml failed validation: {error}") from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render private scheduler configuration from .env")
    parser.add_argument("--env-file", default=".env", help="operator-managed env file")
    parser.add_argument(
        "--phase",
        choices=("infra", "experiment", "all"),
        default="all",
        help="render infrastructure parameters, experiment inputs, or both",
    )
    parser.add_argument("--check-only", action="store_true", help="validate without writing files")
    parser.add_argument("--force", action="store_true", help="overwrite generated files after review")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        env = _load_environment(args.env_file)
        missing: list[str] = []
        if args.phase in {"infra", "all"}:
            missing.extend(_infra_missing(env))
        if args.phase in {"experiment", "all"}:
            missing.extend(_experiment_missing(env))
        if missing:
            unique = list(dict.fromkeys(missing))
            print("bootstrap cannot continue; fill these .env values:", file=sys.stderr)
            for name in unique:
                print(f"- {name}", file=sys.stderr)
            return 2
        if args.check_only:
            print(f".env passed {args.phase} validation; no files were written")
            return 0
        if args.phase in {"infra", "all"}:
            _render_infrastructure(env, force=args.force)
        if args.phase in {"experiment", "all"}:
            _render_experiment(env, force=args.force)
        print("bootstrap completed; review generated files before any -Apply command")
        return 0
    except (BootstrapError, OSError, yaml.YAMLError) as error:
        print(f"bootstrap failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
