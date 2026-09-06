from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from carbon_scheduler.schemas import (  # noqa: E402
    ActionType,
    ScheduleCandidate,
    SchedulingRequest,
)
from carbon_scheduler.milp import solve_schedule  # noqa: E402


class MilpSchedulingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.start = datetime(2026, 9, 6, 9, tzinfo=timezone.utc)
        self.deadline = self.start + timedelta(minutes=30)

    def candidate(
        self,
        region: str,
        *,
        at: datetime | None = None,
        intensity: float = 1.0,
        latency: float = 1.0,
        region_available: bool = True,
        reset_ready: bool = True,
    ) -> ScheduleCandidate:
        return ScheduleCandidate(
            target_region=region,
            scheduled_time=at or self.start,
            carbon_intensity_gco2eq_per_kwh=intensity,
            expected_p95_latency_ms=latency,
            region_available=region_available,
            reset_ready=reset_ready,
        )

    def request(self, candidates: list[ScheduleCandidate]) -> SchedulingRequest:
        return SchedulingRequest(
            workload_profile="test-profile",
            default_region="region-a",
            earliest_start=self.start,
            deadline=self.deadline,
            p95_slo_limit_ms=5.0,
            candidates=candidates,
        )

    def test_selects_lowest_intensity_feasible_candidate(self) -> None:
        result = solve_schedule(
            self.request(
                [
                    self.candidate("region-a", intensity=4.0),
                    self.candidate("region-b", intensity=2.0),
                ]
            )
        )

        self.assertEqual(result.status, "selected")
        self.assertEqual(result.target_region, "region-b")
        self.assertEqual(result.action_type, ActionType.RUN_IN_REGION)
        self.assertEqual(result.carbon_intensity_gco2eq_per_kwh, 2.0)

    def test_deadline_excludes_later_candidate(self) -> None:
        result = solve_schedule(
            self.request(
                [
                    self.candidate("region-a", intensity=4.0),
                    self.candidate(
                        "region-b",
                        at=self.deadline + timedelta(seconds=1),
                        intensity=1.0,
                    ),
                ]
            )
        )

        self.assertEqual(result.status, "selected")
        self.assertEqual(result.target_region, "region-a")

    def test_slo_excludes_unsafe_region(self) -> None:
        result = solve_schedule(
            self.request(
                [
                    self.candidate("region-a", intensity=4.0),
                    self.candidate("region-b", intensity=1.0, latency=6.0),
                ]
            )
        )

        self.assertEqual(result.status, "selected")
        self.assertEqual(result.target_region, "region-a")

    def test_returns_infeasible_when_no_candidate_meets_constraints(self) -> None:
        result = solve_schedule(
            self.request(
                [
                    self.candidate("region-a", region_available=False),
                    self.candidate("region-b", reset_ready=False),
                ]
            )
        )

        self.assertEqual(result.status, "infeasible")
        self.assertIsNone(result.target_region)
        self.assertIn("region unavailable", result.reason or "")

    def test_ties_are_deterministic(self) -> None:
        result = solve_schedule(
            self.request(
                [
                    self.candidate("region-b", intensity=1.0),
                    self.candidate("region-a", intensity=1.0),
                ]
            )
        )

        self.assertEqual(result.status, "selected")
        self.assertEqual(result.target_region, "region-a")
        self.assertEqual(result.action_type, ActionType.RUN_NOW)


if __name__ == "__main__":
    unittest.main()
