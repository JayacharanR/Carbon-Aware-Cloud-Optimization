"""Container entry point for one configured scheduler run.

The capstone prototype has no always-on request API.  The controller process is
started with a completed replay trace and runs its configured cycles once,
leaving JSONL artifacts on the mounted volume.  A higher-level CronJob or
workflow engine may invoke it again for another trace.
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    required = {
        "EXPERIMENT_CONFIG": os.environ.get("EXPERIMENT_CONFIG", ""),
        "EXPERIMENT_MODE": os.environ.get("EXPERIMENT_MODE", ""),
    }
    missing = [name for name, value in required.items() if not value.strip()]
    if missing:
        print(
            "controller requires environment variables: " + ", ".join(missing),
            file=sys.stderr,
        )
        return 2
    try:
        from scripts.run_experiment import main as run_experiment_main
    except ImportError as error:
        print(
            "controller image must include the repository scripts package to run the one-shot entry point",
            file=sys.stderr,
        )
        print(str(error), file=sys.stderr)
        return 2

    argv = [
        "--config",
        required["EXPERIMENT_CONFIG"],
        "--mode",
        required["EXPERIMENT_MODE"],
        "--run-root",
        os.environ.get("RUN_ARTIFACT_ROOT", "/var/lib/carbon-scheduler/runs"),
    ]
    optional_args = {
        "REPLAY_TRACE_PATH": "--replay-trace",
        "GRAPH_PATH": "--graph",
        "TARGET_CONFIG_PATH": "--target-config",
        # The experiment YAML is mounted separately from the pilot-derived
        # latency catalog.  Pass the mounted catalog path explicitly instead
        # of assuming the author's local path exists inside the pod.
        "LATENCY_CATALOG_PATH": "--latency-catalog",
    }
    for variable, flag in optional_args.items():
        value = os.environ.get(variable, "").strip()
        if value:
            argv.extend([flag, value])
    if os.environ.get("CONTROLLER_DRY_RUN", "").lower() in {"1", "true", "yes"}:
        argv.append("--dry-run")
    if os.environ.get("CONTROLLER_DEMO_AGENT", "").lower() in {"1", "true", "yes"}:
        argv.append("--demo-agent")
    demo_evaluator = os.environ.get("CONTROLLER_DEMO_EVALUATOR", "").strip()
    if demo_evaluator:
        argv.extend(["--demo-evaluator", demo_evaluator])
    return int(run_experiment_main(argv))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
