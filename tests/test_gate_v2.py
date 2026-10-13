"""Tests for the v2 trust gate and for keeping the as_built gate unchanged."""

from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from requests.adapters import BaseAdapter

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from carbon_scheduler.agent import (  # noqa: E402
    AgentContextOverflowError,
    DemoProposalAgent,
    OllamaStructuredAgent,
    build_scheduler_prompt,
)
from carbon_scheduler.evaluation import RagasScores  # noqa: E402
from carbon_scheduler.executor import DryRunExecutor  # noqa: E402
from carbon_scheduler.retrieval import ContextRetriever, InMemoryContextStore  # noqa: E402
from carbon_scheduler.schemas import (  # noqa: E402
    ActionProposal,
    ActionType,
    CarbonDataSource,
    CarbonPoint,
    CarbonRegionData,
    CarbonSnapshot,
    DecisionInput,
    DecisionMode,
    DependencyContext,
    GateProfile,
    SnapshotMode,
    TrustComponentScores,
    TrustPolicy,
    TrustWeights,
)
from carbon_scheduler.milp import solve_schedule  # noqa: E402
from carbon_scheduler.trust import (  # noqa: E402
    GatePolicy,
    check_carbon_regret,
    check_data_age,
    check_evidence,
    check_model_feasibility,
    check_zone_provenance,
    evaluate_trust_v2,
)
from carbon_scheduler.workflow import (  # noqa: E402
    SchedulerWorkflow,
    build_scheduling_request,
    trim_snapshot_to_window,
)

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
ZONES = {"region-a": "ZONE-A", "region-b": "ZONE-B"}


def region_data(zone: str, current: float, forecast: list[float], *, observed: datetime = T0) -> CarbonRegionData:
    return CarbonRegionData(
        provider_region=zone,
        source=CarbonDataSource.REPLAY,
        current=CarbonPoint(timestamp=observed, carbon_intensity_gco2eq_per_kwh=current),
        forecast=[
            CarbonPoint(timestamp=T0 + timedelta(minutes=5 * (index + 1)), carbon_intensity_gco2eq_per_kwh=value)
            for index, value in enumerate(forecast)
        ],
        captured_at=observed,
    )


def decision_input(regions: dict[str, CarbonRegionData] | None = None, **changes: object) -> DecisionInput:
    regions = regions or {
        "region-a": region_data("ZONE-A", 100.0, [100.0, 100.0]),
        "region-b": region_data("ZONE-B", 50.0, [50.0, 60.0]),
    }
    values: dict[str, object] = dict(
        run_id="run",
        cycle_id="cycle",
        workload_profile="profile",
        earliest_start=T0,
        deadline=T0 + timedelta(minutes=10),
        candidate_regions=["region-a", "region-b"],
        carbon_snapshot=CarbonSnapshot(snapshot_id="s", mode=SnapshotMode.REPLAY, decision_time=T0, regions=regions),
        dependency_context=DependencyContext(target_service="frontend"),
        retrieval_context_ids=["ev-1", "ev-2"],
        p95_slo_limit_ms=100.0,
        error_rate_limit=0.1,
        cluster_available={"region-a": True, "region-b": True},
        config_hash="config",
    )
    values.update(changes)
    return DecisionInput(**values)


def proposal(**changes: object) -> ActionProposal:
    values: dict[str, object] = dict(
        action_type=ActionType.RUN_IN_REGION,
        target_region="region-b",
        target_service="frontend",
        scheduled_time=None,
        workload_profile="profile",
        expected_p95_latency_ms=10.0,
        self_reported_confidence=0.5,
        evidence_ids=["ev-1"],
        rationale="test",
        model_name="m",
        prompt_version="v",
        request_id="r",
    )
    values.update(changes)
    return ActionProposal.model_validate(values)


POLICY = TrustPolicy(
    threshold=0.6,
    weights=TrustWeights(
        ragas_faithfulness=0.2,
        ragas_context_precision=0.2,
        nemo_rails=0.2,
        data_freshness=0.2,
        execution_feasibility=0.2,
    ),
)
GRAPH = {"service": "frontend", "service_exists": True, "allowed_services": ["frontend"], "dependencies": []}
LATENCY = {"profile": {"region-a": 10.0, "region-b": 10.0}}


class FixedAgent:
    def __init__(self, value: ActionProposal) -> None:
        self.value = value

    def propose(self, decision_input: DecisionInput, **_: object) -> ActionProposal:
        return self.value


class CountingEvaluator:
    def __init__(self, faithfulness: float, precision: float) -> None:
        self.scores = RagasScores(faithfulness, precision)
        self.calls = 0

    def evaluate(self, **_: object) -> RagasScores:
        self.calls += 1
        return self.scores


def workflow(agent: object, evaluator: object, profile: GateProfile, **gate: object) -> SchedulerWorkflow:
    return SchedulerWorkflow(
        agent=agent,  # type: ignore[arg-type]
        trust_evaluator=evaluator,  # type: ignore[arg-type]
        executor=DryRunExecutor({"region-a": "a", "region-b": "b"}),
        trust_policy=POLICY,
        default_region="region-a",
        latency_catalog=LATENCY,
        slot_minutes=5,
        gate=GatePolicy(profile=profile, expected_provider_regions=ZONES, **gate),  # type: ignore[arg-type]
    )


def run(flow: SchedulerWorkflow, di: DecisionInput | None = None):  # noqa: ANN201
    return flow.run_cycle(
        mode=DecisionMode.HYBRID, decision_input=di or decision_input(), graph_context=GRAPH, retrieval_documents=["doc"]
    )


class ScoreAndRailTests(unittest.TestCase):
    def test_as_built_passes_zero_ragas_v2_enforces_threshold(self) -> None:
        good = proposal()  # region-b now, 50 = MILP optimum
        as_built = run(workflow(FixedAgent(good), CountingEvaluator(0.0, 0.0), GateProfile.AS_BUILT))
        self.assertTrue(as_built.trust_report.passed)
        self.assertAlmostEqual(as_built.trust_report.weighted_score, 0.6)
        v2 = run(workflow(FixedAgent(good), CountingEvaluator(0.0, 0.0), GateProfile.V2))
        self.assertFalse(v2.trust_report.passed)
        self.assertEqual(v2.trust_report.weighted_score, 0.0)
        self.assertTrue(v2.execution_receipt.fallback_triggered)

    def test_v2_threshold_is_configurable(self) -> None:
        components = TrustComponentScores(
            ragas_faithfulness=0.5, ragas_context_precision=0.5, nemo_rails=1.0, data_freshness=1.0, execution_feasibility=1.0
        )
        for threshold, expected in ((0.3, True), (0.5, True), (0.6, False), (0.9, False)):
            report = evaluate_trust_v2(
                proposal(), components=components, policy=POLICY.model_copy(update={"threshold": threshold})
            )
            self.assertEqual(report.passed, expected, threshold)
            self.assertAlmostEqual(report.weighted_score, 0.5)

    def test_v2_hard_rail_failure_skips_ragas_and_cannot_be_overridden(self) -> None:
        evaluator = CountingEvaluator(1.0, 1.0)
        bad = proposal(expected_p95_latency_ms=1000.0)  # violates the SLO rail
        result = run(workflow(FixedAgent(bad), evaluator, GateProfile.V2))
        self.assertFalse(result.trust_report.passed)
        self.assertIn("latency_slo", result.trust_report.failure_reason)
        self.assertEqual(evaluator.calls, 0)
        self.assertTrue(result.execution_receipt.fallback_triggered)
        as_built_eval = CountingEvaluator(1.0, 1.0)
        run(workflow(FixedAgent(bad), as_built_eval, GateProfile.AS_BUILT))
        self.assertEqual(as_built_eval.calls, 1)  # as_built still pays for RAGAS first

    def test_v2_accepts_a_good_proposal_and_records_profile(self) -> None:
        result = run(workflow(FixedAgent(proposal()), CountingEvaluator(1.0, 1.0), GateProfile.V2))
        self.assertTrue(result.trust_report.passed)
        self.assertFalse(result.execution_receipt.fallback_triggered)
        record = result.to_record()
        self.assertEqual(record["gate_profile"], "v2")
        self.assertEqual(record["execution_receipt"]["gate_profile"], "v2")
        self.assertEqual(record["trust_report"]["gate_profile"], "v2")
        self.assertEqual(record["trust_report"]["carbon_regret"], 0.0)
        self.assertEqual(record["agent_proposal"]["evidence_ids"], ["ev-1"])

    def test_rejected_llm_proposal_is_kept_in_record(self) -> None:
        bad = proposal(expected_p95_latency_ms=1000.0)
        for profile in GateProfile:
            record = run(workflow(FixedAgent(bad), CountingEvaluator(1.0, 1.0), profile)).to_record()
            self.assertEqual(record["proposal"]["model_name"], "pulp-milp")
            self.assertEqual(record["agent_proposal"]["expected_p95_latency_ms"], 1000.0)
            self.assertEqual(record["gate_profile"], profile.value)


class DataAgeTests(unittest.TestCase):
    def test_age_from_observation_timestamps(self) -> None:
        stale = decision_input(
            {
                "region-a": region_data("ZONE-A", 100.0, [], observed=T0 - timedelta(hours=3)),
                "region-b": region_data("ZONE-B", 50.0, []),
            }
        )
        check, age = check_data_age(stale.carbon_snapshot, max_age_seconds=7200)
        self.assertFalse(check.passed)
        self.assertEqual(age, 3 * 3600)
        fresh, _ = check_data_age(decision_input().carbon_snapshot, max_age_seconds=7200)
        self.assertTrue(fresh.passed)
        future = decision_input(
            {
                "region-a": region_data("ZONE-A", 100.0, [], observed=T0 + timedelta(hours=1)),
                "region-b": region_data("ZONE-B", 50.0, []),
            }
        )
        self.assertFalse(check_data_age(future.carbon_snapshot, max_age_seconds=7200)[0].passed)

    def test_stale_unlabelled_data_passes_as_built_but_not_v2(self) -> None:
        stale = decision_input(
            {
                "region-a": region_data("ZONE-A", 100.0, [], observed=T0 - timedelta(hours=48)),
                "region-b": region_data("ZONE-B", 50.0, [], observed=T0 - timedelta(hours=48)),
            }
        )
        as_built = run(workflow(FixedAgent(proposal()), CountingEvaluator(1, 1), GateProfile.AS_BUILT), stale)
        self.assertTrue(as_built.trust_report.passed)
        self.assertEqual(as_built.trust_report.components.data_freshness, 1.0)
        v2 = run(workflow(FixedAgent(proposal()), CountingEvaluator(1, 1), GateProfile.V2), stale)
        self.assertFalse(v2.trust_report.passed)
        self.assertIn("data_age", v2.trust_report.failure_reason)
        self.assertEqual(v2.trust_report.data_age_seconds, 48 * 3600)


class ZoneProvenanceTests(unittest.TestCase):
    def check(self, regions: dict[str, CarbonRegionData], expected: dict[str, str] = ZONES):  # noqa: ANN201
        return check_zone_provenance(
            decision_input(regions).carbon_snapshot,
            regions=["region-a", "region-b"],
            expected_provider_regions=expected,
            earliest_start=T0,
            deadline=T0 + timedelta(minutes=10),
        )

    def test_legitimate_records_pass(self) -> None:
        self.assertTrue(self.check(decision_input().carbon_snapshot.regions).passed)

    def test_in_so_class_is_rejected(self) -> None:
        corrupted = {name: region_data("IN-SO", 563.0, [563.0, 563.0]) for name in ("region-a", "region-b")}
        check = self.check(corrupted)
        self.assertFalse(check.passed)
        self.assertIn("configured zone", check.reason)
        self.assertIn("same zone", check.reason)
        self.assertIn("identical carbon series", check.reason)

    def test_identical_series_with_distinct_names_is_rejected(self) -> None:
        # the real invalid pilot trace: provider names differ, values identical
        same = {
            "region-a": region_data("eastus", 563.0, [563.0, 563.0]),
            "region-b": region_data("westus2", 563.0, [563.0, 563.0]),
        }
        check = self.check(same, expected={})
        self.assertFalse(check.passed)
        self.assertIn("identical carbon series", check.reason)

    def test_v2_falls_back_and_as_built_accepts_corrupted_inputs(self) -> None:
        corrupted = decision_input({name: region_data("IN-SO", 563.0, [563.0, 563.0]) for name in ("region-a", "region-b")})
        good = proposal(target_region="region-a", action_type=ActionType.RUN_NOW)
        as_built = run(workflow(FixedAgent(good), CountingEvaluator(1, 1), GateProfile.AS_BUILT), corrupted)
        self.assertTrue(as_built.trust_report.passed)
        v2 = run(workflow(FixedAgent(good), CountingEvaluator(1, 1), GateProfile.V2), corrupted)
        self.assertFalse(v2.trust_report.passed)
        self.assertIn("zone_provenance", v2.trust_report.failure_reason)


class CarbonRegretAndEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.di = decision_input()
        self.request = build_scheduling_request(
            self.di, default_region="region-a", latency_catalog=LATENCY, slot_minutes=5
        )
        self.reference = solve_schedule(self.request)

    def test_regret_beyond_tolerance_is_rejected_and_recorded(self) -> None:
        worse = proposal(target_region="region-a", action_type=ActionType.RUN_NOW)  # 100 vs optimum 50
        check, proposal_ci, reference_ci, regret = check_carbon_regret(
            worse, request=self.request, reference=self.reference, tolerance=0.05
        )
        self.assertFalse(check.passed)
        self.assertEqual((proposal_ci, reference_ci, regret), (100.0, 50.0, 1.0))
        ok, *_ = check_carbon_regret(proposal(), request=self.request, reference=self.reference, tolerance=0.05)
        self.assertTrue(ok.passed)
        later = proposal(scheduled_time=T0 + timedelta(minutes=10), action_type=ActionType.DELAY_UNTIL)  # 60: +20 %
        self.assertFalse(check_carbon_regret(later, request=self.request, reference=self.reference, tolerance=0.05)[0].passed)
        self.assertTrue(check_carbon_regret(later, request=self.request, reference=self.reference, tolerance=0.25)[0].passed)

    def test_regret_rail_forces_fallback_in_v2_only(self) -> None:
        worse = proposal(target_region="region-a", action_type=ActionType.RUN_NOW)
        v2 = run(workflow(FixedAgent(worse), CountingEvaluator(1, 1), GateProfile.V2))
        self.assertTrue(v2.execution_receipt.fallback_triggered)
        self.assertEqual(v2.trust_report.carbon_regret, 1.0)
        as_built = run(workflow(FixedAgent(worse), CountingEvaluator(1, 1), GateProfile.AS_BUILT))
        self.assertFalse(as_built.execution_receipt.fallback_triggered)

    def test_missing_reference_fails_closed(self) -> None:
        check, *_ = check_carbon_regret(proposal(), request=None, reference=None, tolerance=0.05)
        self.assertFalse(check.passed)

    def test_evidence_ids_must_be_supplied_ids(self) -> None:
        self.assertTrue(check_evidence(proposal(evidence_ids=[]), allowed_evidence_ids=["ev-1"]).passed)
        self.assertTrue(check_evidence(proposal(evidence_ids=["ev-1"]), allowed_evidence_ids=["ev-1"]).passed)
        bad = check_evidence(proposal(evidence_ids=["made-up"]), allowed_evidence_ids=["ev-1"])
        self.assertFalse(bad.passed)
        self.assertIn("made-up", bad.reason)


class ModelFeasibilityTests(unittest.TestCase):
    """Opt-in rail: the proposal must be feasible under the MILP's own model."""

    SLOW_B = {"profile": {"region-a": 10.0, "region-b": 500.0}}  # region-b catalog P95 > SLO (100)

    def _flow(self, **gate: object) -> SchedulerWorkflow:
        flow = workflow(FixedAgent(proposal()), CountingEvaluator(1.0, 1.0), GateProfile.V2, **gate)
        flow.latency_catalog = self.SLOW_B
        return flow

    def test_default_v2_admits_under_reported_latency_rail_rejects_it(self) -> None:
        # proposal: region-b, self-reported P95 10 ms; catalog says 500 ms > SLO 100 ms
        default = run(self._flow())
        self.assertFalse(default.execution_receipt.fallback_triggered)  # documented gap of the default gate
        self.assertLess(default.trust_report.carbon_regret, 0.0)  # valued below the feasible optimum
        guarded = run(self._flow(model_feasibility_rail=True))
        self.assertTrue(guarded.execution_receipt.fallback_triggered)
        self.assertEqual(guarded.execution_receipt.final_action.target_region, "region-a")
        failed = [c for c in guarded.trust_report.rail_checks if not c.passed]
        self.assertEqual([c.name for c in failed], ["model_feasibility"])
        self.assertIn("exceeds p95 SLO", failed[0].reason)

    def test_feasible_proposal_passes_and_unavailable_region_fails(self) -> None:
        di = decision_input()
        request = build_scheduling_request(di, default_region="region-a", latency_catalog=LATENCY, slot_minutes=5)
        self.assertTrue(check_model_feasibility(proposal(), request=request).passed)
        later = proposal(scheduled_time=T0 + timedelta(minutes=7), action_type=ActionType.DELAY_UNTIL)
        self.assertTrue(check_model_feasibility(later, request=request).passed)  # valued at the 5-min slot
        down = build_scheduling_request(
            decision_input(cluster_available={"region-a": True, "region-b": False}),
            default_region="region-a", latency_catalog=LATENCY, slot_minutes=5,
        )
        self.assertFalse(check_model_feasibility(proposal(), request=down).passed)
        outside = proposal(scheduled_time=T0 + timedelta(hours=1), action_type=ActionType.DELAY_UNTIL)
        self.assertFalse(check_model_feasibility(outside, request=request).passed)
        self.assertFalse(check_model_feasibility(proposal(), request=None).passed)

    def test_rail_is_off_by_default(self) -> None:
        # v2 keeps it off (ablation); v2.1, the default profile, turns it on
        self.assertFalse(GatePolicy(profile=GateProfile.V2).feasibility_rail_enabled)
        self.assertTrue(GatePolicy().feasibility_rail_enabled)


class _FakeOllama(BaseAdapter):
    def __init__(self, prompt_eval_count: int | None = None) -> None:
        super().__init__()
        self.prompt_eval_count = prompt_eval_count
        self.payloads: list[dict] = []

    def send(self, request, **kwargs):  # noqa: ANN001
        payload = json.loads(request.body)
        self.payloads.append(payload)
        policy = json.loads(payload["messages"][1]["content"])
        content = {
            "action_type": "run_in_region",
            "target_region": "region-b",
            "target_service": "frontend",
            "workload_profile": "profile",
            "expected_p95_latency_ms": 10.0,
            "self_reported_confidence": 0.5,
            "evidence_ids": [],
            "rationale": "x",
            "model_name": policy["required_model_name"],
            "prompt_version": policy["required_prompt_version"],
            "request_id": policy["request_id"],
        }
        body = {"message": {"content": json.dumps(content)}}
        if self.prompt_eval_count is not None:
            body["prompt_eval_count"] = self.prompt_eval_count
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(body).encode()
        response.request = request
        return response

    def close(self) -> None:
        pass


def _agent(fake: _FakeOllama, **kwargs: object) -> OllamaStructuredAgent:
    session = requests.Session()
    session.mount("http://", fake)
    return OllamaStructuredAgent(base_url="http://fake", model="m", timeout_seconds=5, retry_count=2, session=session, **kwargs)  # type: ignore[arg-type]


class ContextOverflowTests(unittest.TestCase):
    def test_num_ctx_is_sent_and_as_built_sends_no_options(self) -> None:
        fake = _FakeOllama(prompt_eval_count=100_000)
        _agent(fake, num_ctx=8192, compact_prompt=True).propose(decision_input(), graph_context=GRAPH, retrieval_context="x")
        self.assertEqual(fake.payloads[0]["options"], {"num_ctx": 8192})
        plain = _FakeOllama()
        _agent(plain).propose(decision_input(), graph_context=GRAPH, retrieval_context="x")
        self.assertNotIn("options", plain.payloads[0])

    def test_preflight_overflow_fails_without_calling_the_model(self) -> None:
        fake = _FakeOllama()
        with self.assertRaises(AgentContextOverflowError):
            _agent(fake, num_ctx=2048).propose(decision_input(), graph_context=GRAPH, retrieval_context="x" * 20_000)
        self.assertEqual(fake.payloads, [])

    def test_truncated_evaluation_is_detected_and_not_retried(self) -> None:
        fake = _FakeOllama(prompt_eval_count=10)  # far fewer tokens than the prompt has
        with self.assertRaises(AgentContextOverflowError):
            _agent(fake, num_ctx=8192).propose(decision_input(), graph_context=GRAPH, retrieval_context="x" * 8000)
        self.assertEqual(len(fake.payloads), 1)

    def test_overflow_fails_closed_with_explicit_reason(self) -> None:
        agent = _agent(_FakeOllama(), num_ctx=2048)
        di = decision_input()
        result = workflow(agent, CountingEvaluator(1, 1), GateProfile.V2).run_cycle(
            mode=DecisionMode.HYBRID, decision_input=di, graph_context=GRAPH, retrieval_documents=["x" * 20_000]
        )
        self.assertTrue(result.execution_receipt.fallback_triggered)
        self.assertTrue(result.trust_report.failure_reason.startswith("context_overflow:"))
        llm_only = workflow(agent, None, GateProfile.V2).run_cycle(
            mode=DecisionMode.LLM_ONLY, decision_input=di, graph_context=GRAPH, retrieval_documents=["x" * 20_000]
        )
        self.assertEqual(llm_only.outcome, "proposal_unavailable")
        self.assertTrue(llm_only.failure_reason.startswith("context_overflow:"))

    def test_compact_prompt_trims_forecast_and_lists_evidence(self) -> None:
        di = decision_input(deadline=T0 + timedelta(minutes=5))
        full = json.loads(build_scheduler_prompt(di, graph_context=GRAPH, retrieval_context="", request_id="r", model_name="m"))
        compact = json.loads(
            build_scheduler_prompt(di, graph_context=GRAPH, retrieval_context="", request_id="r", model_name="m", compact=True)
        )
        self.assertNotIn("allowed_evidence_ids", full)
        self.assertEqual(len(full["carbon"]["region-b"]["forecast"]), 2)
        self.assertEqual(len(compact["carbon"]["region-b"]["forecast"]), 1)
        self.assertEqual(compact["allowed_evidence_ids"], ["ev-1", "ev-2"])


class RetrievalAndTrimTests(unittest.TestCase):
    def test_lexical_top8_loses_snapshots_latest_per_source_does_not(self) -> None:
        from scripts.run_experiment import _add_context, retrieve_decision_context

        retriever = ContextRetriever(InMemoryContextStore())
        graph = {"service": "nginx-thrift", "allowed_services": ["nginx-thrift"]}
        query = "pilot nginx-thrift carbon region deadline latency"
        for cycle in range(12):
            when = T0 + timedelta(hours=cycle)
            regions = {
                "region-a": region_data("ZONE-A", 100.0 + cycle, [], observed=when),
                "region-b": region_data("ZONE-B", 50.0, [], observed=when),
            }
            di = decision_input(
                carbon_snapshot=CarbonSnapshot(snapshot_id=f"s{cycle}", mode=SnapshotMode.REPLAY, decision_time=when, regions=regions),
                earliest_start=when,
                deadline=when + timedelta(minutes=10),
            )
            ids = _add_context(retriever, di, graph)
        as_built, docs = retrieve_decision_context(
            retriever, query_text=query, as_of=when, run_id="run", gate_profile=GateProfile.AS_BUILT
        )
        self.assertEqual({record.source_type for record in as_built}, {"dependency-graph"})
        v2, v2_docs = retrieve_decision_context(retriever, query_text=query, as_of=when, run_id="run", gate_profile=GateProfile.V2)
        self.assertEqual([record.source_type for record in v2], ["carbon-snapshot", "dependency-graph"])
        self.assertEqual({record.record_id for record in v2}, set(ids))  # this decision's own documents
        self.assertTrue(all(doc.startswith("[evidence_id=") for doc in v2_docs))
        self.assertFalse(any(doc.startswith("[evidence_id=") for doc in docs))

    def test_trimming_does_not_change_the_milp_decision(self) -> None:
        regions = {
            "region-a": region_data("ZONE-A", 100.0, [90.0, 80.0, 10.0, 5.0]),
            "region-b": region_data("ZONE-B", 50.0, [40.0, 70.0, 1.0, 1.0]),
        }
        di = decision_input(regions, deadline=T0 + timedelta(minutes=10))
        trimmed = di.model_copy(
            update={"carbon_snapshot": trim_snapshot_to_window(di.carbon_snapshot, earliest_start=T0, deadline=di.deadline, slot_minutes=5)}
        )
        self.assertEqual(len(trimmed.carbon_snapshot.regions["region-a"].forecast), 2)
        for value in (di, trimmed):
            request = build_scheduling_request(value, default_region="region-a", latency_catalog=LATENCY, slot_minutes=5)
            result = solve_schedule(request)
            self.assertEqual((result.target_region, result.carbon_intensity_gco2eq_per_kwh), ("region-b", 40.0))


class RecordingExecutor(DryRunExecutor):
    def __init__(self) -> None:
        super().__init__({"region-a": "a", "region-b": "b"})
        self.calls = 0

    def execute(self, **kwargs: object):  # noqa: ANN201
        self.calls += 1
        return super().execute(**kwargs)


def at(hours: float, regions: dict[str, CarbonRegionData] | None = None, cycle: str = "c") -> DecisionInput:
    """Decision input at T0 + hours with fresh (observed at decision time) data."""

    when = T0 + timedelta(hours=hours)
    regions = regions or {
        "region-a": region_data("ZONE-A", 100.0, [], observed=when),
        "region-b": region_data("ZONE-B", 50.0, [], observed=when),
    }
    return decision_input(
        carbon_snapshot=CarbonSnapshot(snapshot_id=f"s-{hours}", mode=SnapshotMode.REPLAY, decision_time=when, regions=regions),
        earliest_start=when,
        deadline=when + timedelta(minutes=10),
        cycle_id=cycle,
    )


def in_so(hours: float, cycle: str = "c") -> DecisionInput:
    when = T0 + timedelta(hours=hours)
    corrupted = {name: region_data("IN-SO", 563.0, [], observed=when) for name in ("region-a", "region-b")}
    return at(hours, corrupted, cycle)


def stale(hours: float, cycle: str = "c") -> DecisionInput:
    when = T0 + timedelta(hours=hours)
    old = when - timedelta(hours=48)
    regions = {
        "region-a": region_data("ZONE-A", 100.0, [], observed=old),
        "region-b": region_data("ZONE-B", 50.0, [], observed=old),
    }
    return at(hours, regions, cycle)


class LastKnownGoodAndHoldTests(unittest.TestCase):
    def flow(self, profile: GateProfile = GateProfile.V2, **gate: object) -> tuple[SchedulerWorkflow, RecordingExecutor, CountingEvaluator]:
        executor = RecordingExecutor()
        evaluator = CountingEvaluator(1.0, 1.0)
        flow = SchedulerWorkflow(
            agent=FixedAgent(proposal()),
            trust_evaluator=evaluator,
            executor=executor,
            trust_policy=POLICY,
            default_region="region-a",
            latency_catalog=LATENCY,
            slot_minutes=5,
            gate=GatePolicy(profile=profile, expected_provider_regions=ZONES, **gate),  # type: ignore[arg-type]
        )
        return flow, executor, evaluator

    def test_in_so_with_prior_clean_snapshot_uses_last_known_good(self) -> None:
        flow, executor, _ = self.flow()
        first = run(flow, at(0, cycle="c1"))
        self.assertEqual(first.decision_source, "llm")
        self.assertTrue(first.data_integrity["passed"])
        result = run(flow, in_so(1, cycle="c2"))
        self.assertEqual(result.decision_source, "milp_last_known_good")
        self.assertEqual(result.outcome, "dry_run")
        self.assertEqual(result.last_known_good, {"snapshot_id": "s-0", "age_seconds": 3600.0})
        # decided on the clean data (region-b 50), not the corrupted IN-SO series
        self.assertEqual(result.schedule_result.target_region, "region-b")
        self.assertEqual(result.schedule_result.carbon_intensity_gco2eq_per_kwh, 50.0)
        self.assertIsNone(result.agent_proposal)  # the LLM is not consulted on failing inputs
        self.assertEqual(result.data_integrity["failed_rails"], ["zone_provenance"])
        record = result.to_record()
        self.assertEqual(record["execution_receipt"]["decision_source"], "milp_last_known_good")
        # a last-known-good decision is flagged, at a lower level than a hold
        self.assertTrue(result.alert)
        self.assertEqual(result.alert_level, "warning")
        self.assertTrue(record["alert"])
        self.assertEqual(record["alert_level"], "warning")
        self.assertTrue(record["execution_receipt"]["alert"])
        self.assertEqual(record["execution_receipt"]["alert_level"], "warning")
        # the clean first cycle raised no alert
        self.assertFalse(first.alert)
        self.assertIsNone(first.alert_level)
        self.assertIsNone(first.to_record()["execution_receipt"]["alert_level"])
        self.assertEqual(executor.calls, 2)

    def test_in_so_without_prior_clean_snapshot_holds_without_executing(self) -> None:
        flow, executor, evaluator = self.flow()
        result = run(flow, in_so(0))
        self.assertEqual(result.outcome, "data_integrity_hold")
        self.assertEqual(result.decision_source, "data_integrity_hold")
        self.assertTrue(result.alert)
        self.assertEqual(executor.calls, 0)  # hold is a no-op: no executor call
        self.assertEqual(evaluator.calls, 0)
        self.assertEqual(result.alert_level, "critical")
        record = result.to_record()
        self.assertEqual(record["alert_level"], "critical")
        receipt = record["execution_receipt"]
        self.assertTrue(receipt["alert"])
        self.assertEqual(receipt["alert_level"], "critical")
        self.assertEqual(receipt["decision_source"], "data_integrity_hold")
        self.assertEqual(receipt["final_action"]["target_region"], "region-a")  # first cycle: static region
        self.assertIn("zone_provenance", result.failure_reason)
        self.assertIn("no last-known-good snapshot", result.failure_reason)
        self.assertIsNone(result.schedule_result)

    def test_too_old_last_known_good_holds_and_keeps_previous_placement(self) -> None:
        flow, executor, _ = self.flow(last_known_good_max_age_seconds=7200)
        first = run(flow, at(0, cycle="c1"))
        self.assertEqual(first.execution_receipt.final_action.target_region, "region-b")
        lkg = run(flow, in_so(1, cycle="c2"))
        self.assertEqual(lkg.decision_source, "milp_last_known_good")
        self.assertEqual((lkg.alert, lkg.alert_level), (True, "warning"))
        held = run(flow, in_so(3, cycle="c3"))  # last-known-good data is now 3 h old > 2 h
        self.assertEqual(held.decision_source, "data_integrity_hold")
        self.assertEqual((held.alert, held.alert_level), (True, "critical"))
        self.assertIn("too old", held.failure_reason)
        self.assertEqual(held.execution_receipt.final_action.target_region, "region-b")  # previous placement
        self.assertEqual(executor.calls, 2)

    def test_stale_data_follows_the_same_logic(self) -> None:
        flow, _, _ = self.flow()
        self.assertEqual(run(flow, stale(0, cycle="c0")).decision_source, "data_integrity_hold")
        run(flow, at(1, cycle="c1"))
        result = run(flow, stale(2, cycle="c2"))
        self.assertEqual(result.decision_source, "milp_last_known_good")
        self.assertEqual(result.data_integrity["failed_rails"], ["data_age"])
        self.assertEqual(result.last_known_good["age_seconds"], 3600.0)
        self.assertEqual((result.alert, result.alert_level), (True, "warning"))

    def test_llm_failures_still_use_milp_on_current_data(self) -> None:
        flow, _, _ = self.flow()
        flow.agent = FixedAgent(proposal(target_region="region-a", action_type=ActionType.RUN_NOW))  # regret 100 %
        result = run(flow, at(0))
        self.assertEqual(result.decision_source, "milp")
        self.assertTrue(result.execution_receipt.fallback_triggered)
        self.assertIsNone(result.last_known_good)
        # an LLM-proposal rejection is routine fail-closed behaviour, not a data alert
        self.assertEqual((result.alert, result.alert_level), (False, None))
        self.assertIsNone(result.execution_receipt.alert_level)
        self.assertEqual(result.decision_input.carbon_snapshot.snapshot_id, "s-0")

    def test_as_built_is_unaffected(self) -> None:
        flow, executor, evaluator = self.flow(GateProfile.AS_BUILT)
        result = run(flow, in_so(0))
        self.assertEqual(result.outcome, "dry_run")
        self.assertIsNone(result.data_integrity)
        self.assertIsNone(result.last_known_good)
        self.assertFalse(result.alert)
        self.assertIsNone(result.alert_level)
        self.assertIsNone(result.to_record()["execution_receipt"]["alert_level"])
        self.assertEqual(result.decision_source, "llm")
        self.assertEqual((executor.calls, evaluator.calls), (1, 1))

    def test_state_can_be_restored_for_resumed_runs(self) -> None:
        flow, _, _ = self.flow()
        flow.restore_state(last_known_good=at(0).carbon_snapshot, last_placement="region-b")
        self.assertEqual(run(flow, in_so(1)).decision_source, "milp_last_known_good")
        fresh, _, _ = self.flow()
        fresh.restore_state(last_known_good=None, last_placement="region-b")
        held = run(fresh, in_so(1))
        self.assertEqual(held.execution_receipt.final_action.target_region, "region-b")


class ConfigTests(unittest.TestCase):
    def test_gate_section_is_optional_and_defaults_to_v21(self) -> None:
        from carbon_scheduler.settings import GateSettings

        self.assertEqual(GateSettings().profile, GateProfile.V2_1)
        self.assertEqual(GateSettings(profile="as_built").profile, GateProfile.AS_BUILT)
        self.assertEqual(SchedulerWorkflow(
            agent=None, trust_evaluator=None, executor=DryRunExecutor({}), trust_policy=POLICY,
            default_region="region-a", latency_catalog=LATENCY,
        ).gate.profile, GateProfile.V2_1)

    def test_demo_agent_hybrid_still_runs_under_both_profiles(self) -> None:
        for profile in GateProfile:
            result = run(workflow(DemoProposalAgent(expected_p95_latency_ms=10), CountingEvaluator(1, 1), profile))
            self.assertEqual(result.outcome, "dry_run")


if __name__ == "__main__":
    unittest.main()
