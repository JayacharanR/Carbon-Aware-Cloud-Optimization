"""Validate a completed experiment before cloud mutation or main runs."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import requests

try:  # direct script invocation
    from _common import REPO_ROOT, required_environment, resolve_path  # type: ignore
except ImportError:  # package invocation
    from scripts._common import REPO_ROOT, required_environment, resolve_path  # type: ignore

from carbon_scheduler.carbon_client import ElectricityMapsClient  # noqa: E402
from carbon_scheduler.settings import ConfigurationError, load_experiment_config  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run non-mutating scheduler preflight checks")
    parser.add_argument("--config", required=True, help="completed experiment YAML")
    parser.add_argument("--skip-live", action="store_true", help="skip Electricity Maps requests")
    parser.add_argument("--skip-ollama", action="store_true", help="skip Ollama reachability")
    parser.add_argument("--skip-tools", action="store_true", help="skip local CLI/manifest checks")
    parser.add_argument("--skip-azure", action="store_true", help="skip az subscription check")
    return parser


def _check_tools() -> list[str]:
    failures: list[str] = []
    for tool in ("python", "docker", "git"):
        if shutil.which(tool) is None:
            failures.append(f"required local command not found: {tool}")
    for path in (
        REPO_ROOT / "deploy" / "deathstarbench" / "benchmark-job.yaml",
        REPO_ROOT / "deploy" / "deathstarbench" / "reset-job.yaml",
        REPO_ROOT / "infra" / "bicep" / "main.bicep",
    ):
        if not path.exists():
            failures.append(f"required repository asset not found: {path}")
    return failures


def _check_azure() -> list[str]:
    if shutil.which("az") is None:
        return ["Azure CLI (az) is not installed"]
    result = subprocess.run(
        ["az", "account", "show", "--output", "none"],
        capture_output=True,
        text=True,
        check=False,
    )
    return [] if result.returncode == 0 else ["Azure CLI is not logged in or subscription is unavailable"]


def _check_ollama(base_url: str) -> list[str]:
    endpoint = f"{base_url.rstrip('/')}/api/tags"
    try:
        response = requests.get(endpoint, timeout=10)
        response.raise_for_status()
    except requests.RequestException as error:
        return [f"Ollama endpoint check failed: {error}"]
    return []


def _check_live_carbon(config: object) -> list[str]:
    # Type is ExperimentConfig, kept as object here to avoid exposing a second
    # configuration API from the CLI.
    try:
        token = required_environment("ELECTRICITY_MAPS_API_KEY")
        client = ElectricityMapsClient(
            api_token=token,
            cache_directory=resolve_path(  # type: ignore[attr-defined]
                os.environ.get("CARBON_CACHE_DIRECTORY", "").strip()
                or config.carbon.cache_directory
            ),
            cache_max_age=timedelta(seconds=config.carbon.cache_max_age_seconds),  # type: ignore[attr-defined]
            api_base_url=config.carbon.api_base_url,  # type: ignore[attr-defined]
            provider=config.carbon.provider,  # type: ignore[attr-defined]
            forecast_horizon_hours=config.carbon.forecast_horizon_hours,  # type: ignore[attr-defined]
        )
        client.fetch_snapshot(  # type: ignore[attr-defined]
            {
                config.azure.primary_region: config.carbon.primary_provider_region,  # type: ignore[attr-defined]
                config.azure.secondary_region: config.carbon.secondary_provider_region,  # type: ignore[attr-defined]
            }
        )
    except Exception as error:
        return [f"Electricity Maps provider-region preflight failed: {error}"]
    return []


def run_preflight(args: argparse.Namespace) -> int:
    failures: list[str] = []
    try:
        config = load_experiment_config(args.config)
    except ConfigurationError as error:
        print(f"configuration failed: {error}", file=sys.stderr)
        return 1

    if not args.skip_tools:
        failures.extend(_check_tools())
    if not args.skip_azure:
        failures.extend(_check_azure())
    if not args.skip_live:
        failures.extend(_check_live_carbon(config))
    if not args.skip_ollama:
        failures.extend(_check_ollama(config.ollama.base_url))

    if failures:
        print("Preflight failed:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print("Preflight passed. This verifies configured access only; it does not guarantee future Azure capacity.")
    return 0


def main(argv: list[str] | None = None) -> int:
    return run_preflight(build_parser().parse_args(argv))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
