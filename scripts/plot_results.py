"""Create simple descriptive plots from observed run artifacts.

This helper deliberately omits confidence intervals, hypothesis tests, and
imputed values.  A missing observation is left out of the corresponding plot.
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

try:
    from _common import resolve_path  # type: ignore
except ImportError:
    from scripts._common import resolve_path  # type: ignore

from carbon_scheduler.results import (  # noqa: E402
    ArtifactError,
    load_run_records,
    summarize_run_directory,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Plot observed scheduler results without statistical inference")
    parser.add_argument("--runs-dir", default="artifacts/runs")
    parser.add_argument("--output-dir", default="artifacts/plots")
    parser.add_argument("--include-demo", action="store_true")
    return parser


def _finite(value: object) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _load_rows(runs_dir: Path, include_demo: bool) -> list[tuple[dict[str, object], Path]]:
    rows: list[tuple[dict[str, object], Path]] = []
    for child in sorted(runs_dir.iterdir() if runs_dir.exists() else []):
        if not child.is_dir() or not (child / "run-manifest.json").exists():
            continue
        row = summarize_run_directory(child)
        if not include_demo and row.get("demo") is True:
            continue
        rows.append((row, child))
    return rows


def _bar_plot(
    *,
    rows: list[tuple[dict[str, object], Path]],
    field: str,
    ylabel: str,
    destination: Path,
) -> bool:
    values: list[float] = []
    labels: list[str] = []
    for row, _ in rows:
        value = row.get(field)
        if not _finite(value):
            continue
        values.append(float(value))
        labels.append(f"{row.get('manifest_mode', 'unknown')}\n{row.get('run_id', '')[:8]}")
    if not values:
        return False
    import matplotlib.pyplot as plt

    figure, axis = plt.subplots(figsize=(max(6, len(values) * 1.4), 4))
    axis.bar(range(len(values)), values)
    axis.set_xticks(range(len(values)), labels, rotation=30, ha="right")
    axis.set_ylabel(ylabel)
    axis.grid(axis="y", alpha=0.3)
    figure.tight_layout()
    figure.savefig(destination, dpi=150)
    plt.close(figure)
    return True


def _trust_plot(*, rows: list[tuple[dict[str, object], Path]], destination: Path) -> bool:
    values_by_mode: dict[str, list[float]] = {}
    for row, directory in rows:
        mode = str(row.get("manifest_mode", "unknown"))
        for record in load_run_records(directory):
            trust = record.get("trust_report") or {}
            value = trust.get("weighted_score") if isinstance(trust, dict) else None
            if _finite(value):
                values_by_mode.setdefault(mode, []).append(float(value))
    if not values_by_mode:
        return False
    import matplotlib.pyplot as plt

    figure, axis = plt.subplots(figsize=(6, 4))
    axis.hist(
        [values for _, values in sorted(values_by_mode.items())],
        bins=10,
        label=[mode for mode, _ in sorted(values_by_mode.items())],
        alpha=0.7,
    )
    axis.set_xlabel("Observed weighted trust score")
    axis.set_ylabel("Cycle count")
    axis.legend()
    axis.grid(axis="y", alpha=0.3)
    figure.tight_layout()
    figure.savefig(destination, dpi=150)
    plt.close(figure)
    return True


def plot_runs(*, runs_dir: str | Path, output_dir: str | Path, include_demo: bool = False) -> list[Path]:
    try:
        import matplotlib

        matplotlib.use("Agg")
    except ImportError as error:
        raise RuntimeError("plotting requires the optional analysis extra: uv sync --extra analysis") from error
    rows = _load_rows(Path(runs_dir), include_demo)
    if not rows:
        raise ArtifactError("no run manifests found for plotting")
    destination_dir = Path(output_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    plots = [
        ("carbon_intensity_mean.png", "selected_carbon_intensity_mean_gco2eq_per_kwh", "Mean selected carbon intensity (gCO2e/kWh)"),
        ("p95_latency_mean.png", "p95_latency_mean_ms", "Observed mean p95 latency (ms)"),
        ("slo_violation_rate.png", "slo_violation_rate", "Observed SLO violation rate"),
        ("fallback_rate.png", "fallback_rate", "Fallback rate"),
    ]
    for filename, field, ylabel in plots:
        destination = destination_dir / filename
        if _bar_plot(rows=rows, field=field, ylabel=ylabel, destination=destination):
            outputs.append(destination)
    trust_destination = destination_dir / "trust_scores.png"
    if _trust_plot(rows=rows, destination=trust_destination):
        outputs.append(trust_destination)
    if not outputs:
        raise ArtifactError("run artifacts contain no observed values that can be plotted")
    return outputs


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        outputs = plot_runs(
            runs_dir=resolve_path(args.runs_dir),
            output_dir=resolve_path(args.output_dir),
            include_demo=args.include_demo,
        )
    except (ArtifactError, OSError, RuntimeError, ValueError) as error:
        print(f"plotting failed: {error}", file=sys.stderr)
        return 1
    for output in outputs:
        print(f"wrote plot: {output}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
