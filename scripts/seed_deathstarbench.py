"""Render or apply the regional DeathStarBench fixture seed Job.

This command only invokes the upstream graph initialiser. It does not claim to
delete existing databases; operators must establish and record their reset
procedure before using sessions for a fair comparison.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

try:
    from _common import REPO_ROOT, resolve_path  # type: ignore
except ImportError:
    from scripts._common import REPO_ROOT, resolve_path  # type: ignore

from carbon_scheduler.executor import _render_job  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render/apply the DeathStarBench seed Job")
    parser.add_argument("--context", required=True)
    parser.add_argument("--cycle-id", required=True)
    parser.add_argument("--namespace", default="benchmark")
    parser.add_argument("--template", default="deploy/deathstarbench/reset-job.yaml")
    parser.add_argument("--tools-image", required=True)
    parser.add_argument("--graph-dataset", required=True)
    parser.add_argument("--frontend-host", required=True)
    parser.add_argument("--frontend-port", type=int, default=8080)
    parser.add_argument("--active-deadline-seconds", type=int, default=900)
    parser.add_argument("--output", help="rendered Job YAML path; stdout if omitted")
    parser.add_argument("--apply", action="store_true", help="apply to the named context")
    return parser


def render_seed(args: argparse.Namespace) -> dict[str, object]:
    return _render_job(
        template_path=resolve_path(args.template),
        name=f"reset-{args.cycle_id}",
        namespace=args.namespace,
        labels={
            "carbon-scheduler/run-id": args.cycle_id,
            "carbon-scheduler/cycle-id": args.cycle_id,
            "carbon-scheduler/mode": "seed",
            "carbon-scheduler/region": args.context,
        },
        environment={
            "CYCLE_ID": args.cycle_id,
            "DSB_TOOLS_IMAGE": args.tools_image,
            "GRAPH_DATASET": args.graph_dataset,
            "FRONTEND_HOST": args.frontend_host,
            "FRONTEND_PORT": str(args.frontend_port),
            "ACTIVE_DEADLINE_SECONDS": str(args.active_deadline_seconds),
        },
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        body = render_seed(args)
        serialized = yaml_dump(body)
        if args.output:
            output = resolve_path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(serialized, encoding="utf-8")
            print(f"wrote rendered seed Job: {output}")
        else:
            print(serialized, end="")
        if args.apply:
            if not args.output:
                raise ValueError("--output is required with --apply so the reviewed manifest is explicit")
            result = subprocess.run(
                ["kubectl", "--context", args.context, "apply", "-f", str(resolve_path(args.output))],
                check=False,
                text=True,
            )
            return result.returncode
        return 0
    except Exception as error:
        print(f"seed failed: {error}", file=sys.stderr)
        return 1


def yaml_dump(value: object) -> str:
    try:
        import yaml

        return yaml.safe_dump(value, sort_keys=False)
    except ImportError:
        # JSON is valid YAML and keeps the renderer usable in a minimal image.
        return json.dumps(value, indent=2) + "\n"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
