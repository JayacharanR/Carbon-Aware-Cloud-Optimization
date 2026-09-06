"""Run one scheduler mode against a frozen carbon trace.

The default path requires a real replay trace and a completed experiment
configuration.  ``--demo-agent`` and ``--dry-run`` are explicit local plumbing
switches; their artifacts are labelled as demos and must not enter a results
section as experimental evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping

try:  # direct script invocation
    from _common import parse_datetime, resolve_path  # type: ignore
except ImportError:  # package invocation
    from scripts._common import parse_datetime, resolve_path  # type: ignore

from carbon_scheduler.agent import DemoProposalAgent, OllamaStructuredAgent  # noqa: E402
from carbon_scheduler.carbon_client import load_replay_trace  # noqa: E402
from carbon_scheduler.evaluation import RagasScores, RagasOllamaEvaluator  # noqa: E402
from carbon_scheduler.executor import (  # noqa: E402
    DryRunExecutor,
    KubernetesBenchmarkExecutor,
    KubernetesRegionTarget,
)
from carbon_scheduler.graph_ingest import ManifestDependencyGraph  # noqa: E402
from carbon_scheduler.results import RunArtifactWriter  # noqa: E402
from carbon_scheduler.schemas import (  # noqa: E402
    CarbonSnapshot,
    DecisionInput,
    DecisionMode,
    DependencyContext,
)
from carbon_scheduler.settings import configuration_sha256, load_experiment_config  # noqa: E402
from carbon_scheduler.workflow import SchedulerWorkflow, load_latency_catalog  # noqa: E402
from carbon_scheduler.retrieval import (  # noqa: E402
    ContextRetriever,
    InMemoryContextStore,
    QdrantContextStore,
)
from carbon_scheduler.telemetry import NoopTelemetry, configure_otlp  # noqa: E402


class DemoEvaluator:
    """Explicit evaluator for plumbing tests; values must be supplied by caller."""

    def __init__(self, faithfulness: float, context_precision: float) -> None:
        self.scores = RagasScores(faithfulness, context_precision)

    def evaluate(self, **_: Any) -> RagasScores:
        return self.scores


@dataclass(frozen=True, slots=True)
class RunOptions:
    config_path: Path
    mode: DecisionMode
    trace_path: Path
    run_id: str
    run_root: Path
    graph_path: Path | None
    target_config_path: Path | None
    dry_run: bool
    demo_agent: bool
    demo_evaluator: tuple[float, float] | None
    unavailable_regions: frozenset[str]
    max_cycles: int | None
    latency_catalog_path: Path | None = None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one carbon scheduler mode against a replay trace")
    parser.add_argument("--config", required=True, help="completed experiment YAML")
    parser.add_argument(
        "--mode",
        required=True,
        choices=[mode.value for mode in DecisionMode],
        help="algorithm mode; static_reference is the denominator policy",
    )
    parser.add_argument("--replay-trace", help="frozen JSON/JSONL trace; defaults to config carbon.replay_trace_path")
    parser.add_argument("--run-id", help="path-safe run identifier; defaults to a UUID")
    parser.add_argument("--run-root", default="artifacts/runs")
    parser.add_argument("--graph", help="saved ManifestDependencyGraph JSON")
    parser.add_argument(
        "--target-config",
        help="YAML Kubernetes region/template configuration required without --dry-run",
    )
    parser.add_argument(
        "--latency-catalog",
        help="pilot-derived latency catalog; defaults to config milp.latency_catalog_path",
    )
    parser.add_argument("--dry-run", action="store_true", help="do not create Kubernetes Jobs")
    parser.add_argument(
        "--demo-agent",
        action="store_true",
        help="use an explicitly labelled deterministic proposal agent; plumbing only",
    )
    parser.add_argument(
        "--demo-evaluator",
        metavar="FAITHFULNESS,PRECISION",
        help="use explicit evaluator scores for plumbing only; e.g. 1.0,1.0",
    )
    parser.add_argument(
        "--unavailable-region",
        action="append",
        default=[],
        help="mark a configured region unavailable in the decision input (repeatable)",
    )
    parser.add_argument("--max-cycles", type=int, help="run only the first N trace snapshots")
    return parser


def run_from_options(options: RunOptions) -> Path:
    config = load_experiment_config(options.config_path)
    # The container deployment injects the already-secured tunnel endpoint at
    # runtime.  Keep the hashed experiment file unchanged, but use the
    # injected endpoint for model/evaluator calls when present.
    runtime_ollama_url = os.environ.get("OLLAMA_BASE_URL", "").strip()
    if runtime_ollama_url:
        config = config.model_copy(
            update={
                "ollama": config.ollama.model_copy(update={"base_url": runtime_ollama_url})
            }
        )
    config_hash = configuration_sha256(options.config_path)
    if not config.workload.reset_before_session:
        raise ValueError(
            "workload.reset_before_session must be true; each independent regional session requires a reset"
        )
    telemetry = NoopTelemetry()
    otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if otlp_endpoint:
        telemetry = configure_otlp(endpoint=otlp_endpoint)
    with telemetry.span("scheduler.carbon.replay_load"):
        snapshots = load_replay_trace(options.trace_path)
    if options.max_cycles is not None:
        if options.max_cycles < 1:
            raise ValueError("--max-cycles must be at least one")
        snapshots = snapshots[: options.max_cycles]
    if not snapshots:
        raise ValueError("replay trace contains no snapshots")

    regions = [config.azure.primary_region, config.azure.secondary_region]
    configured_regions = set(config.milp.allowed_regions)
    if set(regions) != configured_regions:
        raise ValueError(
            "config milp.allowed_regions must contain exactly the configured primary and secondary Azure regions"
        )
    for snapshot in snapshots:
        if set(snapshot.regions) != set(regions):
            raise ValueError(
                f"trace snapshot {snapshot.snapshot_id} does not contain exactly configured application regions"
            )

    # Repository-relative paths keep config/experiment.yaml portable between
    # local and container runs. Absolute paths remain valid.
    latency_catalog_path = options.latency_catalog_path or resolve_path(
        os.environ.get("LATENCY_CATALOG_PATH", "").strip()
        or config.milp.latency_catalog_path
    )
    latency_catalog = load_latency_catalog(latency_catalog_path)
    profile_latencies = latency_catalog.get(config.workload.profile_id)
    if profile_latencies is None:
        raise ValueError(
            f"latency catalog has no configured workload profile {config.workload.profile_id!r}"
        )
    missing_latency_regions = set(config.milp.allowed_regions) - set(profile_latencies)
    if missing_latency_regions:
        raise ValueError(
            "latency catalog is missing configured region(s) for the workload profile: "
            + ", ".join(sorted(missing_latency_regions))
        )
    earliest_start = parse_datetime(config.workload.earliest_start)
    deadline = parse_datetime(config.workload.deadline)

    with telemetry.span("scheduler.graph.load"):
        graph = _load_graph(
            options.graph_path,
            target_service=config.workload.frontend_service,
            allow_minimal=options.demo_agent or options.dry_run,
        )
    graph_context = graph.context_for(config.workload.frontend_service)
    if not graph_context["service_exists"]:
        raise ValueError(
            f"workload frontend_service {config.workload.frontend_service!r} is not present in the dependency graph"
        )
    dependency_context = DependencyContext(
        target_service=config.workload.frontend_service,
        declared_dependencies=[edge["target"] for edge in graph_context["dependencies"]],
        graph_source_hash=graph_context["graph_hash"],
    )
    unavailable = set(options.unavailable_regions)
    unknown_unavailable = unavailable - set(regions)
    if unknown_unavailable:
        raise ValueError(f"--unavailable-region contains unknown region(s): {sorted(unknown_unavailable)}")
    cluster_available = {region: region not in unavailable for region in regions}

    executor = _build_executor(config, options, regions)
    agent = _build_agent(config, options, latency_catalog)
    evaluator = _build_evaluator(config, options)
    orchestration_backend = os.environ.get(
        "SCHEDULER_ORCHESTRATION_BACKEND", "direct"
    ).strip().lower()
    if orchestration_backend == "langgraph":
        try:
            import langgraph.graph  # noqa: F401
        except ImportError as error:
            raise ValueError(
                "SCHEDULER_ORCHESTRATION_BACKEND=langgraph requires the agent extra"
            ) from error
    workflow = SchedulerWorkflow(
        agent=agent,
        trust_evaluator=evaluator,
        executor=executor,
        trust_policy=config.trust,
        default_region=config.experiments.static_reference_region,
        latency_catalog=latency_catalog,
        slot_minutes=config.milp.slot_minutes,
        orchestration_backend=orchestration_backend,
        telemetry=telemetry,
    )
    retriever_backend = os.environ.get("SCHEDULER_RETRIEVAL_BACKEND", "in_memory").strip().lower()
    if retriever_backend == "qdrant":
        qdrant_url = os.environ.get("QDRANT_URL", "").strip()
        if not qdrant_url:
            raise ValueError("SCHEDULER_RETRIEVAL_BACKEND=qdrant requires QDRANT_URL")
        retriever_store = QdrantContextStore(
            url=qdrant_url,
            api_key=os.environ.get("QDRANT_API_KEY", "").strip() or None,
        )
    elif retriever_backend == "in_memory":
        retriever_store = InMemoryContextStore()
    else:
        raise ValueError(
            "SCHEDULER_RETRIEVAL_BACKEND must be either 'in_memory' or 'qdrant'"
        )
    retriever = ContextRetriever(retriever_store)

    trace_hash = hashlib.sha256(options.trace_path.read_bytes()).hexdigest()
    latency_catalog_hash = hashlib.sha256(latency_catalog_path.read_bytes()).hexdigest()
    target_config_hash = (
        hashlib.sha256(options.target_config_path.read_bytes()).hexdigest()
        if options.target_config_path is not None
        else None
    )
    writer = RunArtifactWriter(
        root=options.run_root,
        run_id=options.run_id,
        manifest={
            "mode": options.mode.value,
            "configuration_path": str(options.config_path),
            "configuration_hash": config_hash,
            "carbon_trace_path": str(options.trace_path),
            "carbon_trace_hash": trace_hash,
            "latency_catalog_path": str(latency_catalog_path),
            "latency_catalog_hash": latency_catalog_hash,
            "target_config_path": str(options.target_config_path)
            if options.target_config_path is not None
            else None,
            "target_config_hash": target_config_hash,
            "graph_hash": graph.digest(),
            "demo": bool(options.demo_agent or options.demo_evaluator or options.dry_run),
            "dry_run": options.dry_run,
            "retrieval_backend": retriever_backend,
            "orchestration_backend": orchestration_backend,
            "notes": (
                "Demo/plumbing artifact; contains no real workload measurements."
                if options.demo_agent or options.demo_evaluator or options.dry_run
                else "Replay experiment artifact; inspect raw records before reporting."
            ),
        },
    )
    # Keep immutable, non-secret inputs beside the raw cycle log so a copied
    # run can be audited without depending on the operator's original paths.
    # Preserve the exact bytes used for each hash.  Text-mode copies can
    # normalize CRLF/LF on Windows and make an otherwise correct manifest hash
    # impossible to verify from the durable run directory.
    writer.write_bytes("experiment-config.yaml", options.config_path.read_bytes())
    writer.write_bytes("latency-catalog.yaml", latency_catalog_path.read_bytes())
    if options.graph_path is not None:
        writer.write_bytes("dependency-graph.json", options.graph_path.read_bytes())
    if options.target_config_path is not None:
        writer.write_bytes("target-config.yaml", options.target_config_path.read_bytes())
    writer.write_bytes("carbon-trace.json", options.trace_path.read_bytes())

    for index, snapshot in enumerate(snapshots):
        cycle_id = f"{options.run_id}-cycle-{index + 1:04d}"
        decision_input = DecisionInput(
            run_id=options.run_id,
            cycle_id=cycle_id,
            workload_profile=config.workload.profile_id,
            earliest_start=earliest_start,
            deadline=deadline,
            candidate_regions=regions,
            carbon_snapshot=snapshot,
            dependency_context=dependency_context,
            retrieval_context_ids=[],
            p95_slo_limit_ms=config.slo.profile_p95_limit_ms,
            error_rate_limit=config.slo.profile_error_rate_limit,
            cluster_available=cluster_available,
            config_hash=config_hash,
        )
        with telemetry.span("scheduler.context.upsert"):
            retrieval_ids = _add_context(retriever, decision_input, graph_context)
        decision_input = decision_input.model_copy(update={"retrieval_context_ids": retrieval_ids})
        with telemetry.span("scheduler.context.retrieve"):
            records = retriever.retrieve(
                query_text=(
                    f"{config.workload.profile_id} {config.workload.frontend_service} "
                    "carbon region deadline latency"
                ),
                as_of=snapshot.decision_time,
                run_id=options.run_id,
                limit=8,
            )
        result = workflow.run_cycle(
            mode=options.mode,
            decision_input=decision_input,
            graph_context=graph_context,
            retrieval_documents=[record.content for record in records],
        )
        writer.append(result.to_record())
        print(
            f"cycle {index + 1}/{len(snapshots)}: outcome={result.outcome} "
            f"fallback={bool(result.execution_receipt and result.execution_receipt.fallback_triggered)}"
        )

    return writer.run_dir


def _load_graph(path: Path | None, *, target_service: str, allow_minimal: bool) -> ManifestDependencyGraph:
    if path is not None:
        graph = ManifestDependencyGraph.load(path)
    elif allow_minimal:
        graph = ManifestDependencyGraph()
        graph.add_node(target_service, kind="configured-target", namespace=None, source_path="demo/configured-target")
    else:
        raise ValueError("--graph is required for a non-demo run; ingest the pinned DeathStarBench manifests first")
    return graph


def _build_agent(config: Any, options: RunOptions, latency_catalog: Mapping[str, Mapping[str, float]]) -> Any:
    if options.mode in {DecisionMode.MILP_ONLY, DecisionMode.STATIC_REFERENCE}:
        return None
    if options.demo_agent:
        values = latency_catalog.get(config.workload.profile_id, {})
        if not values:
            raise ValueError(f"latency catalog has no profile {config.workload.profile_id!r}")
        return DemoProposalAgent(expected_p95_latency_ms=min(values.values()))
    return OllamaStructuredAgent(
        base_url=config.ollama.base_url,
        model=config.ollama.model,
        timeout_seconds=config.ollama.request_timeout_seconds,
        retry_count=config.ollama.retry_count,
    )


def _build_evaluator(config: Any, options: RunOptions) -> Any:
    if options.mode is not DecisionMode.HYBRID:
        return None
    if options.demo_evaluator is not None:
        return DemoEvaluator(*options.demo_evaluator)
    return RagasOllamaEvaluator(
        base_url=config.ollama.base_url,
        model=config.ollama.model,
        timeout_seconds=config.ollama.request_timeout_seconds,
    )


def _build_executor(config: Any, options: RunOptions, regions: list[str]) -> Any:
    if options.dry_run:
        return DryRunExecutor(
            {
                config.azure.primary_region: config.azure.primary_cluster,
                config.azure.secondary_region: config.azure.secondary_cluster,
            }
        )
    if options.target_config_path is None:
        raise ValueError("--target-config is required unless --dry-run is supplied")
    raw = _load_yaml(options.target_config_path)
    region_values = raw.get("regions") if isinstance(raw, dict) else None
    if not isinstance(region_values, dict):
        raise ValueError("target config must contain a regions mapping")
    targets: dict[str, KubernetesRegionTarget] = {}
    for region in regions:
        value = region_values.get(region)
        if not isinstance(value, dict):
            raise ValueError(f"target config has no mapping for region {region}")
        required = [
            "cluster_name",
            "kube_context",
            "namespace",
            "frontend_service",
            "reset_job_template",
            "benchmark_job_template",
        ]
        missing = [key for key in required if not value.get(key)]
        if missing:
            raise ValueError(f"target config for {region} is missing: {', '.join(missing)}")
        template_values = value.get("template_values") or {}
        if not isinstance(template_values, dict):
            raise ValueError(f"target config for {region} template_values must be a mapping")
        normalized_template_values: dict[str, str] = {}
        for key, item in template_values.items():
            if not isinstance(key, str) or item is None or not str(item).strip():
                raise ValueError(
                    f"target config for {region} contains an empty template value for {key!r}"
                )
            normalized_template_values[key] = str(item)
        targets[region] = KubernetesRegionTarget(
            region=region,
            cluster_name=str(value["cluster_name"]),
            kube_context=str(value["kube_context"]),
            namespace=str(value["namespace"]),
            frontend_service=str(value["frontend_service"]),
            reset_job_template=resolve_path(str(value["reset_job_template"])),
            benchmark_job_template=resolve_path(
                str(value["benchmark_job_template"])
            ),
            kubeconfig_path=(
                resolve_path(str(value["kubeconfig_path"]))
                if value.get("kubeconfig_path")
                else None
            ),
            template_values=normalized_template_values,
        )
    in_cluster_regions = [
        target.region
        for target in targets.values()
        if target.kube_context.strip().lower() in {"in-cluster", "in_cluster", "incluster"}
    ]
    if len(in_cluster_regions) > 1:
        raise ValueError(
            "target config marks more than one independent region as in-cluster; "
            "provide a separately mounted kubeconfig/context for the other cluster"
        )
    return KubernetesBenchmarkExecutor(
        targets,
        artifact_root=options.run_root / options.run_id / "benchmark-logs",
    )


def _add_context(
    retriever: ContextRetriever,
    decision_input: DecisionInput,
    graph_context: Mapping[str, Any],
) -> list[str]:
    snapshot = decision_input.carbon_snapshot
    ids = []
    ids.append(
        retriever.add_event(
            run_id=decision_input.run_id,
            event_time=snapshot.decision_time,
            source_type="carbon-snapshot",
            content=json.dumps(snapshot.model_dump(mode="json"), sort_keys=True),
        ).record_id
    )
    ids.append(
        retriever.add_event(
            run_id=decision_input.run_id,
            event_time=snapshot.decision_time,
            source_type="dependency-graph",
            content=json.dumps(dict(graph_context), sort_keys=True, default=str),
        ).record_id
    )
    return ids


def _load_yaml(path: Path) -> Any:
    try:
        import yaml

        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ValueError(f"cannot read YAML file: {path}") from error
    except yaml.YAMLError as error:
        raise ValueError(f"invalid YAML file: {path}") from error


def _parse_demo_evaluator(value: str | None) -> tuple[float, float] | None:
    if value is None:
        return None
    try:
        faithfulness_text, precision_text = value.split(",", 1)
        scores = (float(faithfulness_text), float(precision_text))
    except (ValueError, TypeError) as error:
        raise ValueError("--demo-evaluator must be FAITHFULNESS,PRECISION") from error
    if any(score < 0 or score > 1 for score in scores):
        raise ValueError("demo evaluator scores must be between 0 and 1")
    return scores


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config_path = resolve_path(args.config)
        config = load_experiment_config(config_path)
        trace_arg = args.replay_trace or config.carbon.replay_trace_path
        if not trace_arg:
            raise ValueError("--replay-trace is required when carbon.replay_trace_path is empty")
        options = RunOptions(
            config_path=config_path,
            mode=DecisionMode(args.mode),
            trace_path=resolve_path(trace_arg),
            run_id=args.run_id or str(uuid.uuid4()),
            run_root=resolve_path(args.run_root),
            graph_path=resolve_path(args.graph) if args.graph else None,
            target_config_path=resolve_path(args.target_config)
            if args.target_config
            else None,
            latency_catalog_path=resolve_path(args.latency_catalog)
            if args.latency_catalog
            else None,
            dry_run=args.dry_run,
            demo_agent=args.demo_agent,
            demo_evaluator=_parse_demo_evaluator(args.demo_evaluator),
            unavailable_regions=frozenset(args.unavailable_region),
            max_cycles=args.max_cycles,
        )
        run_dir = run_from_options(options)
        print(f"run artifacts: {run_dir}")
        return 0
    except Exception as error:
        print(f"experiment failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
