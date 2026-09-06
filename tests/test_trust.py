from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from carbon_scheduler.schemas import (  # noqa: E402
    ActionProposal,
    ActionType,
    RailCheck,
    TrustComponentScores,
    TrustPolicy,
    TrustWeights,
)
from carbon_scheduler.trust import (  # noqa: E402
    evaluate_trust,
    should_use_milp_fallback,
    validate_proposal,
)


class TrustGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.start = datetime(2026, 9, 6, 9, tzinfo=timezone.utc)
        self.deadline = self.start + timedelta(minutes=30)
        self.policy = TrustPolicy(
            threshold=0.6,
            weights=TrustWeights(
                ragas_faithfulness=0.2,
                ragas_context_precision=0.2,
                nemo_rails=0.2,
                data_freshness=0.2,
                execution_feasibility=0.2,
            ),
        )

    def proposal(self, **changes: object) -> ActionProposal:
        values: dict[str, object] = {
            "action_type": ActionType.RUN_NOW,
            "target_region": "region-a",
            "target_service": "nginx-thrift",
            "scheduled_time": self.start,
            "workload_profile": "test-profile",
            "expected_p95_latency_ms": 1.0,
            "self_reported_confidence": 0.5,
            "evidence_ids": ["context-1"],
            "rationale": "The supplied context supports this test action.",
            "model_name": "test-model",
            "prompt_version": "test-v1",
            "request_id": "test-request",
        }
        values.update(changes)
        return ActionProposal.model_validate(values)

    @staticmethod
    def complete_components() -> TrustComponentScores:
        return TrustComponentScores(
            ragas_faithfulness=1.0,
            ragas_context_precision=1.0,
            nemo_rails=1.0,
            data_freshness=1.0,
            execution_feasibility=1.0,
        )

    def test_complete_components_pass_and_combine(self) -> None:
        report = evaluate_trust(
            self.proposal(),
            components=self.complete_components(),
            policy=self.policy,
            rail_checks=[RailCheck(name="action_schema", passed=True)],
        )

        self.assertTrue(report.passed)
        self.assertEqual(report.weighted_score, 1.0)
        self.assertFalse(should_use_milp_fallback(report))

    def test_missing_ragas_score_fails_closed(self) -> None:
        components = self.complete_components().model_copy(
            update={"ragas_faithfulness": None}
        )
        report = evaluate_trust(
            self.proposal(), components=components, policy=self.policy
        )

        self.assertFalse(report.passed)
        self.assertIsNone(report.weighted_score)
        self.assertIn("ragas_faithfulness", report.failure_reason or "")
        self.assertTrue(should_use_milp_fallback(report))

    def test_failed_rail_fails_closed(self) -> None:
        report = evaluate_trust(
            self.proposal(),
            components=self.complete_components(),
            policy=self.policy,
            rail_checks=[
                RailCheck(name="target_service", passed=False, reason="not in graph")
            ],
        )

        self.assertFalse(report.passed)
        self.assertIn("target_service", report.failure_reason or "")

    def test_below_threshold_selects_fallback(self) -> None:
        report = evaluate_trust(
            self.proposal(),
            components=TrustComponentScores(
                ragas_faithfulness=0.0,
                ragas_context_precision=0.0,
                nemo_rails=0.0,
                data_freshness=0.0,
                execution_feasibility=0.0,
            ),
            policy=self.policy,
        )

        self.assertFalse(report.passed)
        self.assertEqual(report.weighted_score, 0.0)
        self.assertTrue(should_use_milp_fallback(report))

    def test_evaluator_error_fails_closed(self) -> None:
        report = evaluate_trust(
            self.proposal(),
            components=self.complete_components(),
            policy=self.policy,
            evaluator_error="unavailable",
        )

        self.assertFalse(report.passed)
        self.assertIsNone(report.weighted_score)
        self.assertIn("evaluator failed", report.failure_reason or "")

    def test_invalid_json_cannot_become_a_proposal(self) -> None:
        with self.assertRaises(ValidationError):
            ActionProposal.model_validate_json('{"action_type":"run_now"}')

    def test_deterministic_rails_reject_invalid_region(self) -> None:
        rails = validate_proposal(
            self.proposal(target_region="unknown-region"),
            allowed_regions=["region-a", "region-b"],
            allowed_services=["nginx-thrift"],
            earliest_start=self.start,
            deadline=self.deadline,
            region_available={"region-a": True, "region-b": True},
        )

        self.assertIn("target_region", [rail.name for rail in rails if not rail.passed])

    def test_deterministic_rails_reject_latency_above_slo(self) -> None:
        rails = validate_proposal(
            self.proposal(expected_p95_latency_ms=10.0),
            allowed_regions=["region-a", "region-b"],
            allowed_services=["nginx-thrift"],
            earliest_start=self.start,
            deadline=self.deadline,
            region_available={"region-a": True, "region-b": True},
            p95_slo_limit_ms=5.0,
        )

        self.assertIn("latency_slo", [rail.name for rail in rails if not rail.passed])


if __name__ == "__main__":
    unittest.main()
