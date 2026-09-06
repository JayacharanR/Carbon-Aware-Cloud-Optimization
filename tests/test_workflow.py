from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from carbon_scheduler.agent import DemoProposalAgent  # noqa: E402
from carbon_scheduler.evaluation import RagasScores  # noqa: E402
from carbon_scheduler.executor import DryRunExecutor  # noqa: E402
from carbon_scheduler.schemas import (  # noqa: E402
    CarbonDataSource,
    CarbonPoint,
    CarbonRegionData,
    CarbonSnapshot,
    DependencyContext,
    DecisionInput,
    DecisionMode,
    SnapshotMode,
    TrustPolicy,
    TrustWeights,
)
from carbon_scheduler.workflow import SchedulerWorkflow  # noqa: E402


class FixedEvaluator:
    def evaluate(self, **_: object) -> RagasScores:
        return RagasScores(faithfulness=1.0, context_precision=1.0)


class WorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        regions = {
            region: CarbonRegionData(
                provider_region=region,
                source=CarbonDataSource.REPLAY,
                current=CarbonPoint(
                    timestamp=start,
                    carbon_intensity_gco2eq_per_kwh=value,
                ),
                forecast=[],
                captured_at=start,
            )
            for region, value in (("region-a", 100.0), ("region-b", 50.0))
        }
        snapshot = CarbonSnapshot(
            snapshot_id="snapshot",
            mode=SnapshotMode.REPLAY,
            decision_time=start,
            regions=regions,
        )
        self.input = DecisionInput(
            run_id="run",
            cycle_id="cycle",
            workload_profile="profile",
            earliest_start=start,
            deadline=start + timedelta(minutes=10),
            candidate_regions=["region-a", "region-b"],
            carbon_snapshot=snapshot,
            dependency_context=DependencyContext(target_service="frontend"),
            p95_slo_limit_ms=100.0,
            error_rate_limit=0.1,
            cluster_available={"region-a": True, "region-b": True},
            config_hash="config",
        )
        self.policy = TrustPolicy(
            threshold=0.5,
            weights=TrustWeights(
                ragas_faithfulness=0.2,
                ragas_context_precision=0.2,
                nemo_rails=0.2,
                data_freshness=0.2,
                execution_feasibility=0.2,
            ),
        )
        self.workflow = SchedulerWorkflow(
            agent=DemoProposalAgent(expected_p95_latency_ms=10),
            trust_evaluator=FixedEvaluator(),
            executor=DryRunExecutor({"region-a": "cluster-a", "region-b": "cluster-b"}),
            trust_policy=self.policy,
            default_region="region-a",
            latency_catalog={"profile": {"region-a": 10, "region-b": 10}},
        )
        self.graph = {
            "service": "frontend",
            "service_exists": True,
            "allowed_services": ["frontend"],
            "dependencies": [],
            "graph_hash": "graph",
        }

    def test_hybrid_accepted_proposal_is_not_fallback(self) -> None:
        result = self.workflow.run_cycle(
            mode=DecisionMode.HYBRID,
            decision_input=self.input,
            graph_context=self.graph,
            retrieval_documents=["the supplied carbon policy"],
        )
        self.assertEqual(result.outcome, "dry_run")
        self.assertFalse(result.execution_receipt.fallback_triggered)
        self.assertTrue(result.trust_report.passed)

    def test_hybrid_missing_evaluator_falls_back(self) -> None:
        workflow = SchedulerWorkflow(
            agent=self.workflow.agent,
            trust_evaluator=None,
            executor=self.workflow.executor,
            trust_policy=self.policy,
            default_region="region-a",
            latency_catalog={"profile": {"region-a": 10, "region-b": 10}},
        )
        result = workflow.run_cycle(
            mode=DecisionMode.HYBRID,
            decision_input=self.input,
            graph_context=self.graph,
            retrieval_documents=["the supplied carbon policy"],
        )
        self.assertEqual(result.outcome, "dry_run")
        self.assertTrue(result.execution_receipt.fallback_triggered)
        self.assertFalse(result.trust_report.passed)
        self.assertIsNotNone(result.schedule_result)


if __name__ == "__main__":
    unittest.main()
