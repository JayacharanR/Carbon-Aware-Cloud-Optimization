from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from carbon_scheduler import evaluation  # noqa: E402
from carbon_scheduler.evaluation import RagasOllamaEvaluator  # noqa: E402
from carbon_scheduler.schemas import (  # noqa: E402
    ActionProposal,
    ActionType,
    CarbonDataSource,
    CarbonPoint,
    CarbonRegionData,
    CarbonSnapshot,
    DecisionInput,
    DependencyContext,
    SnapshotMode,
)


class _Score:
    def __init__(self, value: float) -> None:
        self.value = value


class _Metric:
    def __init__(self, llm: object) -> None:
        self.llm = llm

    async def ascore(self, **_: object) -> _Score:
        return _Score(0.75)


class RagasEvaluatorMaxTokensTests(unittest.TestCase):
    def _inputs(self) -> tuple[DecisionInput, ActionProposal]:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        region = CarbonRegionData(
            provider_region="zone",
            source=CarbonDataSource.REPLAY,
            current=CarbonPoint(timestamp=start, carbon_intensity_gco2eq_per_kwh=1.0),
            captured_at=start,
        )
        decision_input = DecisionInput(
            run_id="run",
            cycle_id="cycle",
            workload_profile="profile",
            earliest_start=start,
            deadline=start + timedelta(minutes=5),
            candidate_regions=["region-a"],
            carbon_snapshot=CarbonSnapshot(
                snapshot_id="s", mode=SnapshotMode.REPLAY, decision_time=start, regions={"region-a": region}
            ),
            dependency_context=DependencyContext(target_service="frontend"),
            p95_slo_limit_ms=100.0,
            error_rate_limit=0.1,
            cluster_available={"region-a": True},
            config_hash="h",
        )
        proposal = ActionProposal(
            action_type=ActionType.RUN_NOW,
            target_region="region-a",
            target_service="frontend",
            workload_profile="profile",
            expected_p95_latency_ms=10.0,
            self_reported_confidence=0.5,
            rationale="test",
            model_name="m",
            prompt_version="v",
            request_id="r",
        )
        return decision_input, proposal

    def _run(self, evaluator: RagasOllamaEvaluator) -> tuple[evaluation.RagasScores, dict]:
        captured: dict = {}

        def fake_factory(model: str, **kwargs: object) -> object:
            captured.update(kwargs)
            return object()

        decision_input, proposal = self._inputs()
        with mock.patch.object(
            evaluation, "_import_ragas_dependencies", return_value=(fake_factory, _Metric, _Metric)
        ):
            scores = evaluator.evaluate(
                decision_input=decision_input,
                proposal=proposal,
                retrieval_documents=["context"],
                canonical_policy="{}",
            )
        return scores, captured

    def test_default_keeps_library_max_tokens(self) -> None:
        scores, captured = self._run(
            RagasOllamaEvaluator(base_url="http://127.0.0.1:9", model="m", timeout_seconds=1)
        )
        self.assertNotIn("max_tokens", captured)
        self.assertEqual(scores.faithfulness, 0.75)

    def test_explicit_max_tokens_is_forwarded(self) -> None:
        _, captured = self._run(
            RagasOllamaEvaluator(base_url="http://127.0.0.1:9", model="m", timeout_seconds=1, max_tokens=4096)
        )
        self.assertEqual(captured["max_tokens"], 4096)

    def test_rejects_non_positive_max_tokens(self) -> None:
        with self.assertRaises(ValueError):
            RagasOllamaEvaluator(base_url="http://127.0.0.1:9", model="m", timeout_seconds=1, max_tokens=0)


if __name__ == "__main__":
    unittest.main()
