"""Generate descriptive CSV summaries from raw JSONL run artifacts."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    from _common import resolve_path  # type: ignore
except ImportError:
    from scripts._common import resolve_path  # type: ignore

from carbon_scheduler.results import (  # noqa: E402
    ArtifactError,
    add_static_reference_comparison,
    summarize_run_directory,
    write_summary_csv,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Summarize raw scheduler artifacts without entering metric values")
    parser.add_argument("--runs-dir", default="artifacts/runs")
    parser.add_argument("--output", help="CSV path; defaults to <runs-dir>/summary.csv")
    parser.add_argument("--include-demo", action="store_true", help="include explicitly labelled demo runs")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    runs_dir = resolve_path(args.runs_dir)
    output = resolve_path(args.output) if args.output else runs_dir / "summary.csv"
    try:
        rows = []
        for child in sorted(runs_dir.iterdir() if runs_dir.exists() else []):
            if not child.is_dir() or not (child / "run-manifest.json").exists():
                continue
            row = summarize_run_directory(child)
            if not args.include_demo and row.get("demo") is True:
                continue
            rows.append(row)
        if not rows:
            raise ArtifactError(f"no run manifests found below {runs_dir}")
        destination = write_summary_csv(add_static_reference_comparison(rows), output)
        print(f"wrote descriptive summary: {destination}")
        return 0
    except (ArtifactError, OSError, ValueError) as error:
        print(f"summary failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
