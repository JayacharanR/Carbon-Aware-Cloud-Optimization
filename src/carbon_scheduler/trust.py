"""Fail-closed trust-gate calculations and proposal rail checks."""

from __future__ import annotations

from datetime import datetime
from typing import Iterable

from .schemas import (
    ActionProposal,
    RailCheck,
    TrustComponentScores,
    TrustPolicy,
    TrustReport,
)


def validate_proposal(
    proposal: ActionProposal | None,
    *,
    allowed_regions: Iterable[str],
    allowed_services: Iterable[str],
    earliest_start: datetime,
    deadline: datetime,
    region_available: dict[str, bool],
    p95_slo_limit_ms: float | None = None,
) -> list[RailCheck]:
    """Apply deterministic rails that do not require an evaluator model."""

    if proposal is None:
        return [RailCheck(name="action_schema", passed=False, reason="proposal unavailable")]

    allowed_region_set = set(allowed_regions)
    allowed_service_set = set(allowed_services)
    scheduled_time = proposal.scheduled_time or earliest_start
    checks = [
        RailCheck(name="action_schema", passed=True),
        RailCheck(
            name="target_region",
            passed=proposal.target_region in allowed_region_set,
            reason=None
            if proposal.target_region in allowed_region_set
            else "target region is not configured",
        ),
        RailCheck(
            name="target_service",
            passed=proposal.target_service in allowed_service_set,
            reason=None
            if proposal.target_service in allowed_service_set
            else "target service is not present in dependency context",
        ),
        RailCheck(
            name="latency_estimate",
            passed=proposal.expected_p95_latency_ms > 0,
            reason=None
            if proposal.expected_p95_latency_ms > 0
            else "expected p95 latency is missing",
        ),
        RailCheck(
            name="decision_window",
            passed=earliest_start <= scheduled_time <= deadline,
            reason=None
            if earliest_start <= scheduled_time <= deadline
            else "scheduled time is outside the allowed window",
        ),
        RailCheck(
            name="execution_feasibility",
            passed=region_available.get(proposal.target_region, False),
            reason=None
            if region_available.get(proposal.target_region, False)
            else "target region is unavailable",
        ),
    ]
    if p95_slo_limit_ms is not None:
        checks.append(
            RailCheck(
                name="latency_slo",
                passed=proposal.expected_p95_latency_ms <= p95_slo_limit_ms,
                reason=None
                if proposal.expected_p95_latency_ms <= p95_slo_limit_ms
                else "expected p95 latency exceeds the configured SLO",
            )
        )
    return checks


def evaluate_trust(
    proposal: ActionProposal | None,
    *,
    components: TrustComponentScores,
    policy: TrustPolicy,
    rail_checks: Iterable[RailCheck] = (),
    evaluator_error: str | None = None,
) -> TrustReport:
    """Calculate trust and reject incomplete, invalid, or failed evaluations."""

    checks = list(rail_checks)
    if proposal is None:
        return TrustReport(
            components=components,
            rail_checks=checks,
            weighted_score=None,
            threshold=policy.threshold,
            passed=False,
            failure_reason="proposal unavailable",
        )
    if evaluator_error:
        return TrustReport(
            components=components,
            rail_checks=checks,
            weighted_score=None,
            threshold=policy.threshold,
            passed=False,
            failure_reason=f"evaluator failed: {evaluator_error}",
        )

    missing = components.missing()
    if missing:
        return TrustReport(
            components=components,
            rail_checks=checks,
            weighted_score=None,
            threshold=policy.threshold,
            passed=False,
            failure_reason="missing trust components: " + ", ".join(missing),
        )

    failed_checks = [check.name for check in checks if not check.passed]
    if failed_checks:
        return TrustReport(
            components=components,
            rail_checks=checks,
            weighted_score=None,
            threshold=policy.threshold,
            passed=False,
            failure_reason="failed rails: " + ", ".join(failed_checks),
        )

    component_values = components.model_dump()
    weight_values = policy.weights.model_dump()
    weighted_score = sum(
        float(component_values[name]) * float(weight_values[name])
        for name in weight_values
    )
    passed = weighted_score >= policy.threshold
    return TrustReport(
        components=components,
        rail_checks=checks,
        weighted_score=weighted_score,
        threshold=policy.threshold,
        passed=passed,
        failure_reason=None if passed else "weighted score is below threshold",
    )


def should_use_milp_fallback(report: TrustReport) -> bool:
    """Hybrid mode uses MILP whenever the trust gate did not pass."""

    return not report.passed
