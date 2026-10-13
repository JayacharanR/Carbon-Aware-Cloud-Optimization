"""Fail-closed trust-gate calculations and proposal rail checks.

Two gate profiles exist (``schemas.GateProfile``):

* ``as_built`` - ``validate_proposal`` + ``evaluate_trust``, unchanged.
* ``v2``       - the same seven deterministic rails plus the v2 rails below
  (data age, zone provenance, evidence, carbon regret), and
  ``evaluate_trust_v2``, which scores only the evaluative (RAGAS)
  components so the configured threshold is always enforced.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import combinations
from typing import Iterable, Mapping

from .milp import filter_feasible_candidates
from .schemas import (
    ActionProposal,
    CarbonRegionData,
    CarbonSnapshot,
    GateProfile,
    RailCheck,
    ScheduleResult,
    SchedulingRequest,
    TrustComponentScores,
    TrustPolicy,
    TrustReport,
)

# Components that are evidence of answer quality (scored in v2).  The other
# three (nemo_rails, data_freshness, execution_feasibility) are hard rails in
# v2: they must pass, and they no longer add free credit to the score.
EVALUATIVE_COMPONENTS = ("ragas_faithfulness", "ragas_context_precision")
# Tolerated clock skew for an observation stamped slightly after decision time.
_FUTURE_TOLERANCE = timedelta(seconds=60)


@dataclass(frozen=True, slots=True)
class GatePolicy:
    """Runtime trust-gate settings (built from ``config.gate`` and ``config.carbon``)."""

    profile: GateProfile = GateProfile.V2_1
    max_data_age_seconds: float = 7200.0
    carbon_regret_tolerance: float = 0.05
    # max data age of the last-known-good snapshot used after a data-rail failure
    last_known_good_max_age_seconds: float = 21600.0
    # application region -> configured carbon-provider zone id
    # (config.carbon.primary/secondary_provider_region); empty = not checked.
    expected_provider_regions: Mapping[str, str] = field(default_factory=dict)
    # application region -> the grid zone its carbon data must come from
    # (config.carbon.*_expected_zone).  Needed when the request identifier is
    # not a zone (provider "azure": an Azure region name); empty = compare the
    # returned zone with the requested identifier instead.
    expected_zones: Mapping[str, str] = field(default_factory=dict)
    # The proposal must be a feasible point of the MILP's own model
    # (``check_model_feasibility``).  None = profile default: on for v2.1,
    # off for v2 (kept as the ablation); True/False override explicitly.
    model_feasibility_rail: bool | None = None

    @property
    def feasibility_rail_enabled(self) -> bool:
        if self.model_feasibility_rail is not None:
            return self.model_feasibility_rail
        return self.profile is GateProfile.V2_1


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


# --------------------------------------------------------------------------
# v2 rails


def check_data_age(
    snapshot: CarbonSnapshot, *, max_age_seconds: float, now: datetime | None = None
) -> tuple[RailCheck, float]:
    """Age = reference clock - each region's observation (``current``) timestamp.

    The reference is the snapshot's own ``decision_time`` and, when ``now``
    (the scheduler clock: wall clock live, trace time in replay) is given,
    also ``now``; both must pass.  The snapshot's clock alone is untrusted: a
    replayed old capture carries a consistent old decision_time.  Returns the
    worst age against the snapshot clock (see ``observation_ages`` for the
    scheduler-clock age).  as_built only inspected source labels
    (``workflow._freshness_score``), so a 48 h-old snapshot without a cache
    label counted as fresh.
    """

    clocks = [("snapshot clock", snapshot.decision_time)]
    if now is not None:
        clocks.append(("scheduler clock", now))
    worst = max(observation_ages(snapshot, snapshot.decision_time).values())
    for label, reference in clocks:
        ages = observation_ages(snapshot, reference)
        future = [f"{region}={age:.0f}s" for region, age in ages.items() if age < -_FUTURE_TOLERANCE.total_seconds()]
        stale = [f"{region}={age:.0f}s" for region, age in ages.items() if age > max_age_seconds]
        if future:
            return RailCheck(
                name="data_age", passed=False, reason=f"observation after decision time ({label}): " + ", ".join(future)
            ), worst
        if stale:
            return (
                RailCheck(
                    name="data_age",
                    passed=False,
                    reason=f"observation older than {max_age_seconds:.0f}s ({label}): " + ", ".join(stale),
                ),
                worst,
            )
    return RailCheck(name="data_age", passed=True), worst


def observation_ages(snapshot: CarbonSnapshot, at: datetime) -> dict[str, float]:
    """Seconds between ``at`` and each region's current observation."""

    return {region: (at - data.current.timestamp).total_seconds() for region, data in sorted(snapshot.regions.items())}


def snapshot_data_age_seconds(snapshot: CarbonSnapshot, at: datetime) -> float:
    """Age of a snapshot's oldest observation relative to ``at``."""

    return max((at - data.current.timestamp).total_seconds() for data in snapshot.regions.values())


def _window_series(data: CarbonRegionData, earliest_start: datetime, deadline: datetime) -> tuple:
    points = [(data.current.timestamp.isoformat(), data.current.carbon_intensity_gco2eq_per_kwh)]
    points += [
        (point.timestamp.isoformat(), point.carbon_intensity_gco2eq_per_kwh)
        for point in data.forecast
        if earliest_start <= point.timestamp <= deadline
    ]
    return tuple(points)


def check_zone_provenance(
    snapshot: CarbonSnapshot,
    *,
    regions: Iterable[str],
    expected_provider_regions: Mapping[str, str],
    earliest_start: datetime,
    deadline: datetime,
    expected_zones: Mapping[str, str] | None = None,
) -> RailCheck:
    """Reject carbon records that cannot belong to their region.

    Catches the observed IN-SO mis-mapping class: two application regions
    resolved to the same grid zone (identical series).  Checks: (1) the
    record's zone equals the configured zone when one is configured; (1b,
    v2.1) the zone the provider *returned* (``reported_zone``, when present)
    equals the configured zone, and no two regions were served the same
    returned zone - this catches a single mis-mapped region, a swap and two
    different wrong zones, which (3) cannot; (2) no two regions share a zone;
    (3) no two regions have an identical series (current + forecast points in
    the decision window, >= 2 points, exact float equality).  Check 3
    can in principle reject two genuinely different grids whose values happen
    to coincide over the window (e.g. persistence forecasts with equal current
    values); this is improbable for real data and fails closed.
    """

    regions = list(regions)
    problems: list[str] = []
    for region in regions:
        expected = (expected_provider_regions.get(region) or "").strip()
        actual = snapshot.regions[region].provider_region
        if expected and actual != expected:
            problems.append(f"{region}: record zone {actual!r} != configured zone {expected!r}")
        # v2.1: the zone the provider RETURNED (not the one we requested)
        reported = snapshot.regions[region].reported_zone
        want = ((expected_zones or {}).get(region) or "").strip() or expected or actual
        if reported is not None and reported != want:
            problems.append(f"{region}: provider returned zone {reported!r}, expected {want!r}")
    returned: dict[str, list[str]] = {}
    for region in regions:
        reported = snapshot.regions[region].reported_zone
        if reported is not None:
            returned.setdefault(reported, []).append(region)
    for zone, members in sorted(returned.items()):
        if len(members) > 1:
            problems.append(f"regions {members} were served the same returned zone {zone!r}")
    zones: dict[str, list[str]] = {}
    for region in regions:
        zones.setdefault(snapshot.regions[region].provider_region, []).append(region)
    for zone, members in sorted(zones.items()):
        if len(members) > 1:
            problems.append(f"regions {members} resolve to the same zone {zone!r}")
    series = {region: _window_series(snapshot.regions[region], earliest_start, deadline) for region in regions}
    for first, second in combinations(regions, 2):
        if len(series[first]) >= 2 and series[first] == series[second]:
            problems.append(
                f"{first} and {second} have identical carbon series over the decision window "
                f"({len(series[first])} points)"
            )
    if problems:
        return RailCheck(name="zone_provenance", passed=False, reason="; ".join(problems))
    return RailCheck(name="zone_provenance", passed=True)


def check_evidence(proposal: ActionProposal, *, allowed_evidence_ids: Iterable[str]) -> RailCheck:
    """Cited evidence IDs must be IDs actually supplied to the model.

    An empty citation list passes (grounding quality is scored by RAGAS);
    a fabricated ID fails.
    """

    allowed = set(allowed_evidence_ids)
    unknown = [evidence for evidence in proposal.evidence_ids if evidence not in allowed]
    if unknown:
        return RailCheck(name="evidence_ids", passed=False, reason="unknown evidence ids: " + ", ".join(unknown))
    return RailCheck(name="evidence_ids", passed=True)


def expected_carbon_intensity(proposal: ActionProposal, request: SchedulingRequest) -> float | None:
    """CI the frozen inputs attribute to the proposal's (region, start time).

    Uses the scheduling candidates (the MILP's own valuation): the latest
    candidate slot at or before the start time (piecewise-constant forecast);
    where two candidates share that slot (current value and forecast point at
    earliest_start) the lower value is used, exactly as the MILP would.
    """

    start = proposal.scheduled_time or request.earliest_start
    slots = [
        candidate
        for candidate in request.candidates
        if candidate.target_region == proposal.target_region and candidate.scheduled_time <= start
    ]
    if not slots:
        return None
    latest = max(candidate.scheduled_time for candidate in slots)
    return min(c.carbon_intensity_gco2eq_per_kwh for c in slots if c.scheduled_time == latest)


def check_carbon_regret(
    proposal: ActionProposal,
    *,
    request: SchedulingRequest | None,
    reference: ScheduleResult | None,
    tolerance: float,
) -> tuple[RailCheck, float | None, float | None, float | None]:
    """Reject a proposal whose expected carbon exceeds the MILP optimum by > tolerance.

    BOUND: every proposal accepted by the v2 gate satisfies
        expected_CI(proposal) <= (1 + tolerance) * MILP_optimum_CI
    on the same frozen inputs (forecast values; realized carbon can differ).
    So an accepted LLM proposal can be at most ``tolerance`` (default 5 %)
    worse than the deterministic fallback would have been.

    Returns (check, proposal_ci, reference_ci, relative_regret).
    """

    if request is None or reference is None or reference.status != "selected":
        reason = "no MILP reference" + (
            f": {reference.status}: {reference.reason}" if reference is not None else ""
        )
        return RailCheck(name="carbon_regret", passed=False, reason=reason), None, None, None
    optimum = reference.carbon_intensity_gco2eq_per_kwh
    assert optimum is not None
    proposal_ci = expected_carbon_intensity(proposal, request)
    if proposal_ci is None:
        return (
            RailCheck(name="carbon_regret", passed=False, reason="proposal slot has no carbon value in the frozen inputs"),
            None,
            optimum,
            None,
        )
    if optimum > 0:
        regret: float | None = (proposal_ci - optimum) / optimum
        passed = regret <= tolerance + 1e-12
    else:  # zero-carbon optimum: only an equally zero-carbon proposal is within tolerance
        regret = None if proposal_ci > 0 else 0.0
        passed = proposal_ci <= 0
    reason = None if passed else (
        f"proposal CI {proposal_ci:g} exceeds MILP optimum {optimum:g} by "
        + (f"{regret:.1%}" if regret is not None and math.isfinite(regret) else "an unbounded ratio")
        + f" (tolerance {tolerance:.1%})"
    )
    return RailCheck(name="carbon_regret", passed=passed, reason=reason), proposal_ci, optimum, regret


def check_model_feasibility(proposal: ActionProposal, *, request: SchedulingRequest | None) -> RailCheck:
    """The proposal must be a feasible point of the scheduling model the MILP solves.

    The seven proposal rails check the proposer's *self-reported* P95 estimate
    and the availability flag; the MILP filters candidates with the latency
    catalog's P95 estimate (``milp.filter_feasible_candidates``).  Without
    this rail a proposal for a region whose catalog P95 exceeds the SLO passes
    when the proposer under-reports its latency, and the carbon-regret rail
    then values it below the feasible optimum (negative regret).  The slot
    checked is the one ``expected_carbon_intensity`` values: the latest
    candidate of the proposal's region at or before its start time.
    """

    if request is None:
        return RailCheck(name="model_feasibility", passed=False, reason="no scheduling request")
    start = proposal.scheduled_time or request.earliest_start
    if not request.earliest_start <= start <= request.deadline:
        return RailCheck(name="model_feasibility", passed=False, reason="start time outside the scheduling window")
    slots = [
        candidate
        for candidate in request.candidates
        if candidate.target_region == proposal.target_region and candidate.scheduled_time <= start
    ]
    if not slots:
        return RailCheck(
            name="model_feasibility", passed=False, reason="proposal slot is not a candidate of the scheduling model"
        )
    latest = max(candidate.scheduled_time for candidate in slots)
    chosen = [candidate for candidate in slots if candidate.scheduled_time == latest]
    result = filter_feasible_candidates(request.model_copy(update={"candidates": chosen}))
    if result.feasible:
        return RailCheck(name="model_feasibility", passed=True)
    return RailCheck(
        name="model_feasibility",
        passed=False,
        reason="infeasible under the scheduling model: " + "; ".join(result.excluded_reasons),
    )


def evaluate_trust_v2(
    proposal: ActionProposal | None,
    *,
    components: TrustComponentScores,
    policy: TrustPolicy,
    rail_checks: Iterable[RailCheck] = (),
    evaluator_error: str | None = None,
) -> TrustReport:
    """v2 gate decision: hard rails first, then the threshold on evaluative scores.

    * Any failed rail rejects; no score can override it.
    * The weighted score uses only ``EVALUATIVE_COMPONENTS`` with their
      configured weights renormalised to sum to one.  (as_built added the
      always-1.0 rail/feasibility components and the freshness flag, giving a
      guaranteed 0.4-0.6 that made threshold 0.6 unreachable to fail.)
    * The threshold is then always enforced.
    """

    checks = list(rail_checks)

    def report(*, passed: bool, reason: str | None, score: float | None = None) -> TrustReport:
        return TrustReport(
            components=components,
            rail_checks=checks,
            weighted_score=score,
            threshold=policy.threshold,
            passed=passed,
            failure_reason=reason,
            gate_profile=GateProfile.V2,
        )

    if proposal is None:
        return report(passed=False, reason="proposal unavailable")
    failed = [check.name for check in checks if not check.passed]
    if failed:
        return report(passed=False, reason="failed rails: " + ", ".join(failed))
    if evaluator_error:
        return report(passed=False, reason=f"evaluator failed: {evaluator_error}")
    values = components.model_dump()
    weights = policy.weights.model_dump()
    missing = [name for name in EVALUATIVE_COMPONENTS if values[name] is None]
    if missing:
        return report(passed=False, reason="missing trust components: " + ", ".join(missing))
    total_weight = sum(weights[name] for name in EVALUATIVE_COMPONENTS)
    if total_weight <= 0:
        return report(passed=False, reason="trust weights give no weight to evaluative components")
    score = sum(float(values[name]) * weights[name] for name in EVALUATIVE_COMPONENTS) / total_weight
    score = min(1.0, max(0.0, score))
    passed = score >= policy.threshold
    return report(passed=passed, reason=None if passed else "weighted score is below threshold", score=score)
