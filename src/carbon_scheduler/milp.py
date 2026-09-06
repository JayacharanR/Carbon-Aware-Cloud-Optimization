"""Deterministic carbon-intensity-aware scheduling with a PuLP formulation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .schemas import ActionType, ScheduleCandidate, ScheduleResult, SchedulingRequest

try:
    import pulp
except ImportError:  # pragma: no cover - exercised by dependency-light local runs
    pulp = None  # type: ignore[assignment]


@dataclass(frozen=True)
class FeasibilityResult:
    feasible: tuple[ScheduleCandidate, ...]
    excluded_reasons: tuple[str, ...]


def filter_feasible_candidates(request: SchedulingRequest) -> FeasibilityResult:
    """Apply the non-negotiable scheduling constraints before solving."""

    feasible: list[ScheduleCandidate] = []
    reasons: list[str] = []
    for candidate in request.candidates:
        candidate_id = f"{candidate.target_region}@{candidate.scheduled_time.isoformat()}"
        if candidate.scheduled_time < request.earliest_start:
            reasons.append(f"{candidate_id}: before earliest_start")
        elif candidate.scheduled_time > request.deadline:
            reasons.append(f"{candidate_id}: after deadline")
        elif not candidate.region_available:
            reasons.append(f"{candidate_id}: region unavailable")
        elif not candidate.reset_ready:
            reasons.append(f"{candidate_id}: reset unavailable")
        elif candidate.expected_p95_latency_ms > request.p95_slo_limit_ms:
            reasons.append(f"{candidate_id}: exceeds p95 SLO")
        else:
            feasible.append(candidate)
    return FeasibilityResult(tuple(feasible), tuple(reasons))


def _candidate_sort_key(candidate: ScheduleCandidate) -> tuple[float, object, str]:
    return (
        candidate.carbon_intensity_gco2eq_per_kwh,
        candidate.scheduled_time,
        candidate.target_region,
    )


def _action_type(candidate: ScheduleCandidate, request: SchedulingRequest) -> ActionType:
    if candidate.scheduled_time > request.earliest_start:
        return ActionType.DELAY_UNTIL
    if candidate.target_region == request.default_region:
        return ActionType.RUN_NOW
    return ActionType.RUN_IN_REGION


def _selected_result(
    candidate: ScheduleCandidate,
    request: SchedulingRequest,
    backend: str,
) -> ScheduleResult:
    return ScheduleResult(
        status="selected",
        action_type=_action_type(candidate, request),
        target_region=candidate.target_region,
        scheduled_time=candidate.scheduled_time,
        expected_p95_latency_ms=candidate.expected_p95_latency_ms,
        carbon_intensity_gco2eq_per_kwh=candidate.carbon_intensity_gco2eq_per_kwh,
        objective_value=candidate.carbon_intensity_gco2eq_per_kwh,
        solver_backend=backend,
    )


def _solve_with_pulp(
    candidates: Iterable[ScheduleCandidate],
) -> bool:
    """Run the binary single-choice formulation; selection remains deterministic."""

    if pulp is None:
        return False
    ordered = list(candidates)
    problem = pulp.LpProblem("carbon_intensity_placement", pulp.LpMinimize)
    variables = [pulp.LpVariable(f"choice_{index}", cat="Binary") for index in range(len(ordered))]
    problem += pulp.lpSum(
        candidate.carbon_intensity_gco2eq_per_kwh * variable
        for candidate, variable in zip(ordered, variables, strict=True)
    )
    problem += pulp.lpSum(variables) == 1
    status = problem.solve(pulp.PULP_CBC_CMD(msg=False))
    return pulp.LpStatus[status] == "Optimal"


def solve_schedule(request: SchedulingRequest) -> ScheduleResult:
    """Select one feasible slot with the lowest recorded carbon intensity.

    PuLP is used when installed.  The exact deterministic selection below is also
    used after a successful solve to make ties reproducible across CBC versions.
    """

    feasibility = filter_feasible_candidates(request)
    if not feasibility.feasible:
        reason = "; ".join(feasibility.excluded_reasons) or "no candidates supplied"
        return ScheduleResult(
            status="infeasible",
            solver_backend="pulp" if pulp is not None else "enumeration",
            reason=reason,
        )

    selected = min(feasibility.feasible, key=_candidate_sort_key)
    if pulp is None:
        return _selected_result(selected, request, "enumeration")

    try:
        if not _solve_with_pulp(feasibility.feasible):
            return ScheduleResult(
                status="solver_unavailable",
                solver_backend="pulp",
                reason="PuLP did not return an optimal solution",
            )
    except Exception as error:
        return ScheduleResult(
            status="solver_unavailable",
            solver_backend="pulp",
            reason=f"PuLP execution failed: {error}",
        )
    return _selected_result(selected, request, "pulp")
