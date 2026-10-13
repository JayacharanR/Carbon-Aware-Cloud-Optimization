"""Tests for gate v2.1: model feasibility by default, returned-zone provenance, scheduler clock."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from unittest import mock
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from carbon_scheduler.carbon_client import ElectricityMapsClient, load_replay_trace, reported_zone  # noqa: E402
from carbon_scheduler.executor import DryRunExecutor  # noqa: E402
from carbon_scheduler.schemas import (  # noqa: E402
    ActionType,
    CarbonSnapshot,
    DecisionMode,
    ExecutionReceipt,
    GateProfile,
    SnapshotMode,
)
from carbon_scheduler.settings import GateSettings  # noqa: E402
from carbon_scheduler.trust import GatePolicy, check_data_age, check_zone_provenance  # noqa: E402
from carbon_scheduler.workflow import SchedulerWorkflow  # noqa: E402
from test_gate_v2 import (  # noqa: E402
    GRAPH,
    LATENCY,
    POLICY,
    T0,
    ZONES,
    CountingEvaluator,
    FixedAgent,
    decision_input,
    proposal,
    region_data,
    run,
    workflow,
)


class ProfileTests(unittest.TestCase):
    def test_v21_is_the_default_and_enables_model_feasibility(self) -> None:
        self.assertIs(GatePolicy().profile, GateProfile.V2_1)
        self.assertTrue(GatePolicy().feasibility_rail_enabled)
        self.assertFalse(GatePolicy(profile=GateProfile.V2).feasibility_rail_enabled)
        # explicit switch (E4 ablation) and opt-in under v2
        self.assertFalse(GatePolicy(profile=GateProfile.V2_1, model_feasibility_rail=False).feasibility_rail_enabled)
        self.assertTrue(GatePolicy(profile=GateProfile.V2, model_feasibility_rail=True).feasibility_rail_enabled)
        self.assertIs(GateSettings().profile, GateProfile.V2_1)
        self.assertTrue(GateProfile.V2_1.is_v2_family and GateProfile.V2.is_v2_family)
        self.assertFalse(GateProfile.AS_BUILT.is_v2_family)

    def test_v21_rejects_under_reported_latency_and_records_profile(self) -> None:
        def flow(profile: GateProfile) -> SchedulerWorkflow:
            f = workflow(FixedAgent(proposal()), CountingEvaluator(1.0, 1.0), profile)
            f.latency_catalog = {"profile": {"region-a": 10.0, "region-b": 500.0}}  # region-b catalog P95 > SLO 100
            return f

        v21 = run(flow(GateProfile.V2_1))
        self.assertTrue(v21.execution_receipt.fallback_triggered)
        self.assertIn("model_feasibility", v21.trust_report.failure_reason)
        record = v21.to_record()
        self.assertEqual(record["gate_profile"], "v2.1")
        self.assertEqual(record["execution_receipt"]["gate_profile"], "v2.1")
        v2 = run(flow(GateProfile.V2))
        self.assertFalse(v2.execution_receipt.fallback_triggered)  # the documented v2 gap (ablation)

    def test_v21_keeps_v2_behaviour_on_feasible_proposals(self) -> None:
        for profile in (GateProfile.V2, GateProfile.V2_1):
            result = run(workflow(FixedAgent(proposal()), CountingEvaluator(1.0, 1.0), profile))
            self.assertTrue(result.trust_report.passed, profile)


class ReturnedZoneTests(unittest.TestCase):
    def _snapshot(self, a_zone: str | None, b_zone: str | None, *, swap_values: bool = False) -> CarbonSnapshot:
        a = region_data("ZONE-A", 100.0, [100.0, 101.0])
        b = region_data("ZONE-B", 50.0, [50.0, 60.0])
        if swap_values:
            a, b = (b.model_copy(update={"provider_region": "ZONE-A"}), a.model_copy(update={"provider_region": "ZONE-B"}))
        return CarbonSnapshot(
            snapshot_id="s", mode=SnapshotMode.REPLAY, decision_time=T0,
            regions={"region-a": a.model_copy(update={"reported_zone": a_zone}),
                     "region-b": b.model_copy(update={"reported_zone": b_zone})},
        )

    def _check(self, snapshot: CarbonSnapshot) -> Any:
        return check_zone_provenance(snapshot, regions=["region-a", "region-b"], expected_provider_regions=ZONES,
                                     earliest_start=T0, deadline=T0 + timedelta(minutes=10))

    def test_matching_returned_zones_pass_and_absent_ones_are_not_checked(self) -> None:
        self.assertTrue(self._check(self._snapshot("ZONE-A", "ZONE-B")).passed)
        self.assertTrue(self._check(self._snapshot(None, None)).passed)

    def test_single_mismap_swap_and_two_wrong_zones_are_rejected(self) -> None:
        single = self._check(self._snapshot("ZONE-A", "ZONE-X"))
        self.assertFalse(single.passed)
        self.assertIn("returned zone 'ZONE-X'", single.reason)
        swapped = self._check(self._snapshot("ZONE-B", "ZONE-A", swap_values=True))
        self.assertFalse(swapped.passed)  # series differ, so only the returned zone reveals it
        self.assertFalse(self._check(self._snapshot("ZONE-X", "ZONE-Y")).passed)
        self.assertFalse(self._check(self._snapshot("ZONE-A", "ZONE-A")).passed)
        # without reported zones the swap is invisible (pre-v2.1 provenance)
        self.assertTrue(self._check(self._snapshot(None, None, swap_values=True)).passed)

    def test_azure_lookup_is_judged_against_the_expected_grid_zone(self) -> None:
        # provider "azure": the request names the application region, so the
        # returned zone must be compared with the configured grid zone instead.
        requested = {"region-a": "region-a", "region-b": "region-b"}

        def snap(a_zone: str, b_zone: str) -> CarbonSnapshot:
            a = region_data("region-a", 100.0, [100.0, 101.0]).model_copy(update={"reported_zone": a_zone})
            b = region_data("region-b", 50.0, [50.0, 60.0]).model_copy(update={"reported_zone": b_zone})
            return CarbonSnapshot(snapshot_id="s", mode=SnapshotMode.REPLAY, decision_time=T0,
                                  regions={"region-a": a, "region-b": b})

        def check(snapshot: CarbonSnapshot) -> Any:
            return check_zone_provenance(snapshot, regions=["region-a", "region-b"],
                                         expected_provider_regions=requested, expected_zones=ZONES,
                                         earliest_start=T0, deadline=T0 + timedelta(minutes=10))

        self.assertTrue(check(snap("ZONE-A", "ZONE-B")).passed)  # a correct lookup passes
        wrong = check(snap("IN-WE", "IN-WE"))
        self.assertFalse(wrong.passed)
        self.assertIn("returned zone 'IN-WE', expected 'ZONE-A'", wrong.reason)

    def test_client_records_the_returned_zone(self) -> None:
        self.assertEqual(reported_zone({"zone": "US-NW-BPAT"}, {"zone": "US-NW-BPAT", "forecast": []}), "US-NW-BPAT")
        self.assertEqual(reported_zone({"carbonIntensity": 1}), None)
        self.assertEqual(reported_zone({"zone": "IN-SO"}, {"zone": "US-NW-BPAT"}), "conflict:IN-SO/US-NW-BPAT")

        class Response:
            status_code = 200

            def __init__(self, payload: dict[str, Any]) -> None:
                self.payload = payload

            def json(self) -> dict[str, Any]:
                return self.payload

        class Session:
            def get(self, url: str, **_: Any) -> Response:
                if url.endswith("latest"):
                    return Response({"zone": "IN-SO", "carbonIntensity": 563, "datetime": "2026-09-07T18:00:00Z"})
                return Response({"zone": "IN-SO", "forecast": [
                    {"carbonIntensity": 560, "datetime": "2026-09-07T19:00:00Z"}]})

        with tempfile.TemporaryDirectory() as directory:
            client = ElectricityMapsClient(api_token="t", cache_directory=directory, cache_max_age=timedelta(hours=1),
                                           provider="azure", session=Session())  # type: ignore[arg-type]
            data = client.fetch_region("westus2", now=T0)
        self.assertEqual(data.reported_zone, "IN-SO")
        self.assertEqual(data.provider_region, "westus2")


class _LiveExecutor:
    """Stands in for a real (non-dry-run) executor."""

    def __init__(self) -> None:
        self.calls = 0

    def execute(self, *, action: Any, mode: DecisionMode, run_id: str, cycle_id: str, carbon_snapshot_id: str,
                config_hash: str, fallback_triggered: bool) -> ExecutionReceipt:
        self.calls += 1
        return ExecutionReceipt(run_id=run_id, cycle_id=cycle_id, mode=mode, final_action=action,
                                fallback_triggered=fallback_triggered, started_at=T0, completed_at=T0)


class SchedulerClockTests(unittest.TestCase):
    def test_age_against_the_scheduler_clock_catches_a_replayed_old_capture(self) -> None:
        # a consistent 48 h-old capture: its own decision_time is 1 h after the observation
        old = decision_input()
        old_snapshot = old.carbon_snapshot.model_copy(update={"decision_time": T0 + timedelta(hours=1)})
        check, snapshot_age = check_data_age(old_snapshot, max_age_seconds=7200)
        self.assertTrue(check.passed)
        self.assertEqual(snapshot_age, 3600.0)
        check, _ = check_data_age(old_snapshot, max_age_seconds=7200, now=T0 + timedelta(hours=49))
        self.assertFalse(check.passed)
        self.assertIn("scheduler clock", check.reason)

    def test_dry_run_keeps_trace_time_and_records_both_ages_with_a_clock(self) -> None:
        flow = workflow(FixedAgent(proposal()), CountingEvaluator(1.0, 1.0), GateProfile.V2_1)
        self.assertIsNone(flow.clock)  # DryRunExecutor without a clock: snapshot time only
        flow.clock = lambda: T0 + timedelta(minutes=30)
        result = run(flow)
        self.assertTrue(result.trust_report.passed)
        self.assertEqual(result.trust_report.clock_data_age_seconds, 1800.0)
        self.assertEqual(result.trust_report.data_age_seconds, 0.0)
        self.assertEqual(result.execution_receipt.dispatched_at, T0 + timedelta(minutes=30))

    def _live_flow(self, now: Any) -> tuple[SchedulerWorkflow, _LiveExecutor]:
        executor = _LiveExecutor()
        flow = SchedulerWorkflow(
            agent=FixedAgent(proposal()), trust_evaluator=CountingEvaluator(1.0, 1.0), executor=executor,
            trust_policy=POLICY, default_region="region-a", latency_catalog=LATENCY, slot_minutes=5,
            gate=GatePolicy(profile=GateProfile.V2_1, expected_provider_regions=ZONES), clock=now,
        )
        return flow, executor

    def test_real_execution_after_the_deadline_is_refused(self) -> None:
        flow, executor = self._live_flow(lambda: T0 + timedelta(minutes=11))  # deadline T0 + 10 min
        for mode in (DecisionMode.HYBRID, DecisionMode.MILP_ONLY, DecisionMode.STATIC_REFERENCE):
            result = flow.run_cycle(mode=mode, decision_input=decision_input(), graph_context=GRAPH,
                                    retrieval_documents=["doc"])
            self.assertEqual(result.outcome, "deadline_passed", mode)
            self.assertEqual((result.alert, result.alert_level), (True, "critical"))
            self.assertIn("deadline passed", result.execution_receipt.execution_failure_reason)
        self.assertEqual(executor.calls, 0)

    def test_real_execution_inside_the_window_runs_and_uses_wall_clock_age(self) -> None:
        flow, executor = self._live_flow(lambda: T0 + timedelta(minutes=5))
        result = flow.run_cycle(mode=DecisionMode.HYBRID, decision_input=decision_input(), graph_context=GRAPH,
                                retrieval_documents=["doc"])
        self.assertEqual(result.outcome, "executed")
        self.assertEqual(executor.calls, 1)
        self.assertEqual(result.trust_report.clock_data_age_seconds, 300.0)
        # wall clock 3 h after the observation: the data-age rail fails on the scheduler clock -> hold
        stale_flow, stale_executor = self._live_flow(lambda: T0 + timedelta(hours=3))
        late = decision_input(deadline=T0 + timedelta(hours=4))
        held = stale_flow.run_cycle(mode=DecisionMode.HYBRID, decision_input=late, graph_context=GRAPH,
                                    retrieval_documents=["doc"])
        self.assertEqual(held.decision_source, "data_integrity_hold")
        self.assertEqual(stale_executor.calls, 0)

    def test_default_clock_is_wall_time_for_a_real_executor(self) -> None:
        flow = SchedulerWorkflow(
            agent=None, trust_evaluator=None, executor=_LiveExecutor(), trust_policy=POLICY,
            default_region="region-a", latency_catalog=LATENCY,
        )
        self.assertIsNotNone(flow.clock)
        self.assertTrue(flow.dispatch_checks_deadline)
        dry = SchedulerWorkflow(agent=None, trust_evaluator=None, executor=DryRunExecutor({}), trust_policy=POLICY,
                                default_region="region-a", latency_catalog=LATENCY)
        self.assertIsNone(dry.clock)
        self.assertFalse(dry.dispatch_checks_deadline)

    def test_as_built_keeps_its_original_behaviour_after_the_deadline(self) -> None:
        # as_built is the unchanged baseline: no deadline refusal, but the
        # receipt still records when the action was dispatched.
        executor = _LiveExecutor()
        late = T0 + timedelta(minutes=11)
        flow = SchedulerWorkflow(
            agent=FixedAgent(proposal()), trust_evaluator=CountingEvaluator(1.0, 1.0), executor=executor,
            trust_policy=POLICY, default_region="region-a", latency_catalog=LATENCY, slot_minutes=5,
            gate=GatePolicy(profile=GateProfile.AS_BUILT), clock=lambda: late,
        )
        self.assertFalse(flow.dispatch_checks_deadline)
        result = flow.run_cycle(mode=DecisionMode.MILP_ONLY, decision_input=decision_input(), graph_context=GRAPH,
                                retrieval_documents=["doc"])
        self.assertEqual(result.outcome, "executed")
        self.assertEqual(executor.calls, 1)
        self.assertEqual(result.execution_receipt.dispatched_at, late)
        self.assertEqual(result.to_record()["gate_profile"], "as_built")


class ProductionCliTests(unittest.TestCase):
    """``uv run carbon-scheduler`` (scripts/run_experiment.py) without --dry-run, executor stubbed."""

    def _render_config(self, root: Path, *, earliest: datetime, deadline: datetime) -> Path:
        from scripts import bootstrap

        latency = root / "latency_catalog.yaml"
        values = {
            "AZURE_RESOURCE_GROUP": "rg", "AZURE_PRIMARY_REGION": "eastus2", "AZURE_SECONDARY_REGION": "westus2",
            "AZURE_PRIMARY_CLUSTER": "primary", "AZURE_SECONDARY_CLUSTER": "secondary",
            "CARBON_PROVIDER": "zone", "CARBON_PRIMARY_PROVIDER_REGION": "US-MIDA-PJM",
            "CARBON_SECONDARY_PROVIDER_REGION": "US-NW-BPAT", "CARBON_REPLAY_TRACE_PATH": str(root / "trace.json"),
            "WORKLOAD_FRONTEND_SERVICE": "nginx-thrift", "WORKLOAD_BENCHMARK_SCRIPT": "compose-post.lua",
            "WORKLOAD_PROFILE_ID": "pilot",
            "WORKLOAD_EARLIEST_START": earliest.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "WORKLOAD_DEADLINE": deadline.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "SLO_PROFILE_P95_LIMIT_MS": "500", "SLO_PROFILE_ERROR_RATE_LIMIT": "0.05", "TRUST_THRESHOLD": "0.6",
            "TRUST_WEIGHT_RAGAS_FAITHFULNESS": "0.2", "TRUST_WEIGHT_RAGAS_CONTEXT_PRECISION": "0.2",
            "TRUST_WEIGHT_NEMO_RAILS": "0.2", "TRUST_WEIGHT_DATA_FRESHNESS": "0.2",
            "TRUST_WEIGHT_EXECUTION_FEASIBILITY": "0.2", "MILP_LATENCY_CATALOG_PATH": str(latency),
            "EXPERIMENT_STATIC_REFERENCE_REGION": "eastus2", "EXPERIMENT_RANDOM_SEED": "1",
            "DEATHSTARBENCH_SOURCE": "https://github.com/delimitrou/DeathStarBench.git",
            "DEATHSTARBENCH_COMMIT": "0123456789abcdef0123456789abcdef01234567",
            "DSB_TOOLS_IMAGE": "ghcr.io/example/dsb-tools@sha256:" + "a" * 64,
            "DSB_GRAPH_DATASET": "/opt/deathstarbench/socialNetwork/dataset/graph",
            "DSB_ACTIVE_DEADLINE_SECONDS": "900", "DSB_WRK_THREADS": "2", "DSB_WRK_CONNECTIONS": "2",
            "DSB_WRK_DURATION": "60s", "DSB_REQUESTS_PER_SECOND": "10", "DSB_LOADGEN_CPU_REQUEST": "250m",
            "DSB_LOADGEN_MEMORY_REQUEST": "256Mi",
            "TARGET_PRIMARY_KUBE_CONTEXT": "p", "TARGET_PRIMARY_FRONTEND_SERVICE": "nginx-thrift",
            "TARGET_PRIMARY_FRONTEND_HOST": "h", "TARGET_PRIMARY_FRONTEND_PORT": "8080",
            "TARGET_PRIMARY_TARGET_URL": "http://h:8080",
            "TARGET_SECONDARY_KUBE_CONTEXT": "s", "TARGET_SECONDARY_FRONTEND_SERVICE": "nginx-thrift",
            "TARGET_SECONDARY_FRONTEND_HOST": "h", "TARGET_SECONDARY_FRONTEND_PORT": "8080",
            "TARGET_SECONDARY_TARGET_URL": "http://h:8080",
            "LATENCY_PRIMARY_P95_MS": "120", "LATENCY_SECONDARY_P95_MS": "140",
            "OLLAMA_BASE_URL": "http://127.0.0.1:11434", "OLLAMA_MODEL": "llama3:8b",
        }
        old_root = bootstrap.REPO_ROOT
        bootstrap.REPO_ROOT = root
        try:
            bootstrap._render_experiment(values, force=True)
        finally:
            bootstrap.REPO_ROOT = old_root
        return root / "config" / "experiment.yaml"

    def _trace(self, root: Path, observed: datetime, *, east_zone: str = "US-MIDA-PJM") -> Path:
        def data(zone: str, reported: str, values: list[float]) -> dict[str, Any]:
            return {
                "provider_region": zone, "reported_zone": reported, "source": "live",
                "current": {"timestamp": observed.isoformat(), "carbon_intensity_gco2eq_per_kwh": values[0]},
                "forecast": [{"timestamp": (observed + timedelta(minutes=5 * (i + 1))).isoformat(),
                              "carbon_intensity_gco2eq_per_kwh": v} for i, v in enumerate(values[1:])],
                "captured_at": observed.isoformat(),
            }

        snapshot = {"snapshot_id": "cli-test", "mode": "capture", "decision_time": observed.isoformat(),
                    "regions": {"eastus2": data("US-MIDA-PJM", east_zone, [400.0] * 13),
                                "westus2": data("US-NW-BPAT", "US-NW-BPAT", [100.0] * 13)}}
        path = root / "trace.json"
        path.write_text(json.dumps({"format_version": 1, "snapshots": [snapshot]}))
        return path

    def _run(self, root: Path, config: Path, trace: Path, mode: str, profile: str | None) -> tuple[dict, int]:
        from scripts import run_experiment

        executor = _LiveExecutor()
        options = run_experiment.RunOptions(
            config_path=config, mode=DecisionMode(mode), trace_path=trace, run_id=f"cli-{mode}-{profile}-{len(list((root / 'runs').glob('*'))) if (root / 'runs').exists() else 0}",
            run_root=root / "runs", graph_path=None, target_config_path=None, dry_run=False, demo_agent=True,
            demo_evaluator=(1.0, 1.0), unavailable_regions=frozenset(), max_cycles=1,
            gate_profile=GateProfile(profile) if profile else None,
        )
        env = {"SCHEDULER_RETRIEVAL_BACKEND": "in_memory", "SCHEDULER_ORCHESTRATION_BACKEND": "direct",
               "OTEL_EXPORTER_OTLP_ENDPOINT": "", "LATENCY_CATALOG_PATH": ""}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(run_experiment, "_build_executor", lambda *_args: executor):
            run_dir = run_experiment.run_from_options(options)
        record = json.loads((run_dir / "cycles.jsonl").read_text().splitlines()[-1])
        return record, executor.calls

    def test_cli_without_dry_run_uses_the_wall_clock(self) -> None:
        now = datetime.now(UTC).replace(second=0, microsecond=0)
        earliest = now - timedelta(minutes=now.minute % 5)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # window still open, observation 10 min old on the wall clock
            config = self._render_config(root, earliest=earliest, deadline=earliest + timedelta(minutes=60))
            trace = self._trace(root, earliest - timedelta(minutes=10))
            record, calls = self._run(root, config, trace, "hybrid", "v2.1")
            self.assertEqual(record["gate_profile"], "v2.1")
            self.assertEqual(record["outcome"], "executed")
            self.assertEqual(calls, 1)
            self.assertIsNotNone(record["execution_receipt"]["dispatched_at"])
            self.assertEqual(record["execution_receipt"]["gate_profile"], "v2.1")
            self.assertGreaterEqual(record["trust_report"]["clock_data_age_seconds"], 600.0)
            self.assertEqual(record["trust_report"]["data_age_seconds"], 0.0)  # snapshot clock
            self.assertIn("model_feasibility", [c["name"] for c in record["trust_report"]["rail_checks"]])

            # a returned zone that differs from the configured zone holds the cycle
            wrong = self._trace(root, earliest - timedelta(minutes=10), east_zone="IN-WE")
            record, calls = self._run(root, config, wrong, "hybrid", "v2.1")
            self.assertEqual(record["outcome"], "data_integrity_hold")
            self.assertIn("provider returned zone 'IN-WE'", record["failure_reason"])
            self.assertEqual(calls, 0)

    def test_capture_writes_the_returned_zone_into_the_trace(self) -> None:
        from scripts import capture_carbon

        observed = datetime(2026, 10, 11, 4, 0, tzinfo=UTC)

        class Response:
            status_code = 200

            def __init__(self, payload: dict[str, Any]) -> None:
                self.payload = payload

            def json(self) -> dict[str, Any]:
                return self.payload

        class Session:
            def get(self, url: str, *, params: dict[str, Any], **_: Any) -> Response:
                zone = params["zone"]
                returned = "IN-WE" if zone == "US-NW-BPAT" else zone  # westus2 answered for the wrong grid
                if url.endswith("latest"):
                    return Response({"zone": returned, "carbonIntensity": 300, "datetime": observed.isoformat()})
                return Response({"zone": returned, "forecast": [
                    {"carbonIntensity": 310, "datetime": (observed + timedelta(hours=1)).isoformat()}]})

        real_client = capture_carbon.ElectricityMapsClient
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._render_config(root, earliest=observed, deadline=observed + timedelta(hours=1))
            env = {"ELECTRICITY_MAPS_API_KEY": "test-token", "CARBON_CACHE_DIRECTORY": str(root / "cache")}
            with mock.patch.dict(os.environ, env), mock.patch.object(
                capture_carbon, "ElectricityMapsClient", lambda **kw: real_client(session=Session(), **kw)
            ):
                out = capture_carbon.capture_trace(config_path=config, output_path=root / "captured.json", cycles=1,
                                                   interval_seconds=0, timeout_seconds=5)
            raw = json.loads(out.read_text())["snapshots"][0]["regions"]
            self.assertEqual(raw["eastus2"]["reported_zone"], "US-MIDA-PJM")
            self.assertEqual(raw["westus2"]["reported_zone"], "IN-WE")
            replayed = load_replay_trace(out)[0]
            check = check_zone_provenance(
                replayed, regions=["eastus2", "westus2"],
                expected_provider_regions={"eastus2": "US-MIDA-PJM", "westus2": "US-NW-BPAT"},
                earliest_start=observed, deadline=observed + timedelta(hours=1),
            )
            self.assertFalse(check.passed)
            self.assertIn("westus2: provider returned zone 'IN-WE'", check.reason)

    def test_cli_without_dry_run_refuses_after_the_deadline(self) -> None:
        now = datetime.now(UTC).replace(second=0, microsecond=0)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            earliest = now - timedelta(hours=3)
            config = self._render_config(root, earliest=earliest, deadline=earliest + timedelta(minutes=10))
            trace = self._trace(root, earliest)
            record, calls = self._run(root, config, trace, "milp_only", "v2.1")
            self.assertEqual(record["outcome"], "deadline_passed")
            self.assertEqual(record["alert_level"], "critical")
            self.assertEqual(calls, 0)
            # the snapshot's own clock says the data is fresh; the wall clock says 3 h old
            record, calls = self._run(root, config, trace, "hybrid", "v2.1")
            self.assertEqual(record["outcome"], "data_integrity_hold")
            self.assertIn("scheduler clock", record["failure_reason"])
            self.assertGreaterEqual(record["trust_report"]["clock_data_age_seconds"], 3 * 3600.0)
            self.assertEqual(calls, 0)
            # as_built: unchanged (no refusal, no age check), dispatch time recorded
            record, calls = self._run(root, config, trace, "milp_only", "as_built")
            self.assertEqual(record["outcome"], "executed")
            self.assertEqual(calls, 1)


if __name__ == "__main__":
    unittest.main()
