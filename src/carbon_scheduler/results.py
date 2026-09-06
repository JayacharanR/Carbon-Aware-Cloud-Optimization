"""Auditable run artifacts and lightweight descriptive summaries."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import shutil
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable, Mapping
from uuid import uuid4


class ArtifactError(RuntimeError):
    """An artifact could not be read or safely written."""


class RunArtifactWriter:
    """Append-only JSONL writer with a run manifest and immutable inputs."""

    def __init__(
        self,
        *,
        root: str | Path,
        run_id: str,
        manifest: Mapping[str, Any],
    ) -> None:
        if not run_id or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", run_id):
            raise ValueError("run_id must be a nonempty path-safe value")
        self.run_dir = Path(root) / run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.run_dir / "run-manifest.json"
        self.events_path = self.run_dir / "cycles.jsonl"
        if self.manifest_path.exists():
            raise ArtifactError(f"run manifest already exists: {self.manifest_path}")
        manifest_value = {
            "format_version": 1,
            "run_id": run_id,
            "created_at": datetime.now(UTC).isoformat(),
            **dict(manifest),
        }
        _write_json(self.manifest_path, manifest_value, overwrite=False)

    @property
    def run_id(self) -> str:
        return self.run_dir.name

    def append(self, record: Mapping[str, Any]) -> None:
        if record.get("decision_input", {}).get("run_id") not in {None, self.run_id}:
            raise ArtifactError("cycle record run_id does not match artifact directory")
        payload = json.dumps(dict(record), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        with self.events_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.write("\n")

    def write_input_trace(self, snapshots: Iterable[Mapping[str, Any]]) -> Path:
        destination = self.run_dir / "carbon-trace.json"
        _write_json(destination, list(snapshots), overwrite=False)
        return destination

    def write_text(self, relative_path: str, content: str) -> Path:
        destination = self.run_dir / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise ArtifactError(f"artifact already exists: {destination}")
        destination.write_text(content, encoding="utf-8")
        return destination

    def write_bytes(self, relative_path: str, content: bytes) -> Path:
        """Copy an immutable input without newline/encoding normalization."""

        destination = self.run_dir / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise ArtifactError(f"artifact already exists: {destination}")
        destination.write_bytes(content)
        return destination


def load_run_records(run_dir: str | Path) -> list[dict[str, Any]]:
    path = Path(run_dir) / "cycles.jsonl"
    if not path.exists():
        raise ArtifactError(f"cycle log does not exist: {path}")
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise ArtifactError(f"invalid JSON on line {line_number} of {path}") from error
        if not isinstance(value, dict):
            raise ArtifactError(f"cycle record on line {line_number} is not an object")
        records.append(value)
    return records


def summarize_records(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Calculate only descriptive values present in raw execution records."""

    values = [dict(record) for record in records]
    modes = Counter(str(record.get("mode", "unknown")) for record in values)
    outcomes = Counter(str(record.get("outcome", "unknown")) for record in values)
    fallback_count = sum(
        1
        for record in values
        if bool((record.get("execution_receipt") or {}).get("fallback_triggered"))
    )
    receipts = [record.get("execution_receipt") or {} for record in values]
    p95_values = [float(item["actual_p95_latency_ms"]) for item in receipts if _finite_number(item.get("actual_p95_latency_ms"))]
    error_values = [float(item["actual_error_rate"]) for item in receipts if _finite_number(item.get("actual_error_rate"))]
    executed = sum(1 for item in receipts if item.get("execution_failure_reason") is None and item.get("job_name"))
    slo_values = [item.get("slo_passed") for item in receipts if isinstance(item.get("slo_passed"), bool)]
    selected_intensities: list[float] = []
    carbon_sources: Counter[str] = Counter()
    for record in values:
        decision_input = record.get("decision_input") or {}
        snapshot = decision_input.get("carbon_snapshot") or {}
        regions = snapshot.get("regions") or {}
        for data in regions.values():
            if isinstance(data, dict) and data.get("source"):
                source = str(data["source"])
                if source == "replay" and data.get("fallback_reason"):
                    source = "cache_fallback_replay"
                carbon_sources[source] += 1
        receipt = record.get("execution_receipt") or {}
        # A rejected proposal or an infeasible/failing cycle is not a placed
        # session.  Count intensity only for a receipt that reached the
        # executor (dry-run is retained solely for explicitly included demos).
        if record.get("outcome") not in {"executed", "dry_run"} or not receipt:
            continue
        action = receipt.get("final_action") or {}
        target_region = action.get("target_region")
        scheduled_time = action.get("scheduled_time")
        intensity = _intensity_at(regions.get(target_region), scheduled_time)
        if intensity is not None:
            selected_intensities.append(intensity)

    count = len(values)
    return {
        "cycles": count,
        "modes": ";".join(f"{key}:{value}" for key, value in sorted(modes.items())),
        "outcomes": ";".join(f"{key}:{value}" for key, value in sorted(outcomes.items())),
        "executed_cycles": executed,
        "fallback_cycles": fallback_count,
        "fallback_rate": fallback_count / count if count else None,
        "slo_observed_cycles": len(slo_values),
        "slo_passed_cycles": sum(1 for value in slo_values if value),
        "slo_violation_rate": (
            sum(1 for value in slo_values if not value) / len(slo_values) if slo_values else None
        ),
        "p95_latency_observations": len(p95_values),
        "p95_latency_mean_ms": mean(p95_values) if p95_values else None,
        "p95_latency_median_ms": median(p95_values) if p95_values else None,
        "error_rate_observations": len(error_values),
        "error_rate_mean": mean(error_values) if error_values else None,
        "selected_carbon_intensity_observations": len(selected_intensities),
        "selected_carbon_intensity_mean_gco2eq_per_kwh": mean(selected_intensities)
        if selected_intensities
        else None,
        "carbon_sources": ";".join(f"{key}:{value}" for key, value in sorted(carbon_sources.items())),
    }


def summarize_run_directory(run_dir: str | Path) -> dict[str, Any]:
    directory = Path(run_dir)
    manifest_path = directory / "run-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ArtifactError(f"invalid run manifest: {manifest_path}") from error
    if not isinstance(manifest, dict):
        raise ArtifactError(f"run manifest is not an object: {manifest_path}")
    _verify_copied_inputs(directory, manifest)
    summary = summarize_records(load_run_records(directory))
    return {"run_id": directory.name, **_summary_manifest_fields(manifest), **summary}


def write_summary_csv(rows: Iterable[Mapping[str, Any]], destination: str | Path) -> Path:
    rows_list = [dict(row) for row in rows]
    destination_path = Path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = sorted({key for row in rows_list for key in row})
    with destination_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows_list)
    return destination_path


def add_static_reference_comparison(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Add descriptive intensity differences when a static run is available.

    The calculation uses only means already observed in each run.  If there is
    no static-reference row, or its mean is unavailable/zero, the comparison
    fields remain ``None`` rather than introducing a denominator or estimate.
    """

    copied = [dict(row) for row in rows]
    static_values = [
        row.get("selected_carbon_intensity_mean_gco2eq_per_kwh")
        for row in copied
        if row.get("manifest_mode") == "static_reference"
        and _finite_number(row.get("selected_carbon_intensity_mean_gco2eq_per_kwh"))
    ]
    static_mean = float(static_values[0]) if len(static_values) == 1 else None
    for row in copied:
        chosen = row.get("selected_carbon_intensity_mean_gco2eq_per_kwh")
        difference = None
        reduction = None
        if static_mean is not None and _finite_number(chosen):
            difference = float(chosen) - static_mean
            if static_mean != 0:
                reduction = (static_mean - float(chosen)) / static_mean * 100.0
        row["static_reference_mean_gco2eq_per_kwh"] = static_mean
        row["carbon_intensity_difference_vs_static_gco2eq_per_kwh"] = difference
        row["carbon_intensity_reduction_percent_vs_static"] = reduction
    return copied


def copy_run_artifacts(source: str | Path, destination: str | Path) -> Path:
    """Copy a completed run to a durable location without overwriting files."""

    source_path = Path(source)
    destination_path = Path(destination)
    if not (source_path / "run-manifest.json").exists():
        raise ArtifactError(f"source run manifest does not exist: {source_path}")
    if destination_path.exists():
        raise ArtifactError(f"destination already exists: {destination_path}")
    shutil.copytree(source_path, destination_path)
    return destination_path


def _summary_manifest_fields(manifest: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "manifest_mode": manifest.get("mode"),
        "configuration_hash": manifest.get("configuration_hash"),
        "carbon_trace_hash": manifest.get("carbon_trace_hash"),
        "latency_catalog_hash": manifest.get("latency_catalog_hash"),
        "target_config_hash": manifest.get("target_config_hash"),
        "graph_hash": manifest.get("graph_hash"),
        "retrieval_backend": manifest.get("retrieval_backend"),
        "orchestration_backend": manifest.get("orchestration_backend"),
        "demo": bool(manifest.get("demo", False)),
        "dry_run": bool(manifest.get("dry_run", False)),
        "notes": manifest.get("notes"),
    }


def _finite_number(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _verify_copied_inputs(directory: Path, manifest: Mapping[str, Any]) -> None:
    """Reject summaries when an immutable copied input no longer matches its hash."""

    file_hashes = {
        "carbon-trace.json": manifest.get("carbon_trace_hash"),
        "experiment-config.yaml": manifest.get("configuration_hash"),
        "latency-catalog.yaml": manifest.get("latency_catalog_hash"),
        "target-config.yaml": manifest.get("target_config_hash"),
    }
    for filename, expected in file_hashes.items():
        if expected is None:
            continue
        path = directory / filename
        if not path.exists():
            raise ArtifactError(f"run input copy is missing: {path}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            raise ArtifactError(f"run input hash mismatch for {path}")


def _intensity_at(region_data: Any, scheduled_time: Any) -> float | None:
    if not isinstance(region_data, dict):
        return None
    points: list[tuple[str, float]] = []
    current = region_data.get("current")
    if isinstance(current, dict) and _finite_number(current.get("carbon_intensity_gco2eq_per_kwh")):
        points.append((str(current.get("timestamp")), float(current["carbon_intensity_gco2eq_per_kwh"])))
    for point in region_data.get("forecast") or []:
        if isinstance(point, dict) and _finite_number(point.get("carbon_intensity_gco2eq_per_kwh")):
            points.append((str(point.get("timestamp")), float(point["carbon_intensity_gco2eq_per_kwh"])))
    if not points:
        return None
    target = str(scheduled_time) if scheduled_time is not None else points[0][0]
    target_key = _timestamp_key(target)
    exact = [
        value
        for timestamp, value in points
        if _timestamp_key(timestamp) == target_key
    ]
    return exact[0] if exact else None


def _timestamp_key(value: str) -> object:
    """Compare serialized timestamps by instant, not by offset spelling."""

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except (TypeError, ValueError):
        # Invalid timestamps are already outside the typed replay contract;
        # retain a deterministic string comparison for defensive parsing.
        return value


def _write_json(path: Path, value: Any, *, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise ArtifactError(f"artifact already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()
