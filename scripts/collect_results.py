"""Copy a completed run's immutable artifacts to a durable destination."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    from _common import REPO_ROOT, resolve_path  # type: ignore
except ImportError:
    from scripts._common import REPO_ROOT, resolve_path  # type: ignore

from carbon_scheduler.results import ArtifactError, copy_run_artifacts  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Copy scheduler run artifacts without overwriting existing data")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--run-dir", help="existing run directory")
    source.add_argument("--run-id", help="run ID below the repository artifact root")
    parser.add_argument(
        "--output-directory",
        required=True,
        help="destination directory; must not already exist",
    )
    parser.add_argument("--runs-root", default="artifacts/runs")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        source = (
            resolve_path(args.run_dir)
            if args.run_dir
            else resolve_path(args.runs_root) / str(args.run_id)
        )
        destination_root = resolve_path(args.output_directory)
        destination = destination_root / source.name if destination_root.name != source.name else destination_root
        copied = copy_run_artifacts(source, destination)
        print(f"copied artifacts to {copied}")
        return 0
    except (ArtifactError, OSError, ValueError) as error:
        print(f"collection failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
