"""Typed, serialisable contracts shared by the scheduler components."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ContractModel(BaseModel):
    """Base model that rejects silently ignored fields in controller records."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


def _require_timezone(value: datetime | None) -> datetime | None:
    if value is None:
        return value
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return value


class ActionType(str, Enum):
    RUN_NOW = "run_now"
    RUN_IN_REGION = "run_in_region"
    DELAY_UNTIL = "delay_until"


class DecisionMode(str, Enum):
    LLM_ONLY = "llm_only"
    MILP_ONLY = "milp_only"
    HYBRID = "hybrid"
    STATIC_REFERENCE = "static_reference"


class CarbonDataSource(str, Enum):
    LIVE = "live"
    CACHE = "cache"
    REPLAY = "replay"


class SnapshotMode(str, Enum):
    CAPTURE = "capture"
    REPLAY = "replay"


class CarbonPoint(ContractModel):
    timestamp: datetime
    carbon_intensity_gco2eq_per_kwh: float = Field(ge=0)

    _timestamp_is_timezone_aware = field_validator("timestamp")(_require_timezone)


class CarbonRegionData(ContractModel):
    provider_region: str = Field(min_length=1)
    source: CarbonDataSource
    current: CarbonPoint
    forecast: list[CarbonPoint] = Field(default_factory=list)
    captured_at: datetime
    raw_source_path: str | None = None
    raw_source_hash: str | None = None
    fallback_reason: str | None = None

    _captured_at_is_timezone_aware = field_validator("captured_at")(_require_timezone)

    @model_validator(mode="after")
    def forecast_is_ordered(self) -> "CarbonRegionData":
        timestamps = [point.timestamp for point in self.forecast]
        if timestamps != sorted(timestamps):
            raise ValueError("forecast points must be sorted by timestamp")
        return self


class CarbonSnapshot(ContractModel):
    snapshot_id: str = Field(min_length=1)
    mode: SnapshotMode
    decision_time: datetime
    regions: dict[str, CarbonRegionData] = Field(min_length=1)
    source_hash: str | None = None

    _decision_time_is_timezone_aware = field_validator("decision_time")(_require_timezone)


class DependencyContext(ContractModel):
    target_service: str = Field(min_length=1)
    declared_dependencies: list[str] = Field(default_factory=list)
    graph_source_hash: str | None = None
    source_type: Literal["manifest"] = "manifest"


class DecisionInput(ContractModel):
    run_id: str = Field(min_length=1)
    cycle_id: str = Field(min_length=1)
    workload_profile: str = Field(min_length=1)
    earliest_start: datetime
    deadline: datetime
    candidate_regions: list[str] = Field(min_length=1)
    carbon_snapshot: CarbonSnapshot
    dependency_context: DependencyContext
    retrieval_context_ids: list[str] = Field(default_factory=list)
    p95_slo_limit_ms: float = Field(gt=0)
    error_rate_limit: float = Field(ge=0, le=1)
    cluster_available: dict[str, bool]
    config_hash: str = Field(min_length=1)

    _earliest_is_timezone_aware = field_validator("earliest_start")(_require_timezone)
    _deadline_is_timezone_aware = field_validator("deadline")(_require_timezone)

    @model_validator(mode="after")
    def decision_window_and_regions_are_valid(self) -> "DecisionInput":
        if self.deadline < self.earliest_start:
            raise ValueError("deadline must be at or after earliest_start")
        if len(set(self.candidate_regions)) != len(self.candidate_regions):
            raise ValueError("candidate_regions must not contain duplicates")
        missing_regions = set(self.candidate_regions) - set(self.carbon_snapshot.regions)
        if missing_regions:
            raise ValueError(
                "carbon snapshot is missing candidate regions: "
                + ", ".join(sorted(missing_regions))
            )
        return self


class ActionProposal(ContractModel):
    action_type: ActionType
    target_region: str = Field(min_length=1)
    target_service: str = Field(min_length=1)
    scheduled_time: datetime | None = None
    workload_profile: str = Field(min_length=1)
    expected_p95_latency_ms: float = Field(gt=0)
    self_reported_confidence: float = Field(ge=0, le=1)
    evidence_ids: list[str] = Field(default_factory=list)
    rationale: str = Field(min_length=1)
    model_name: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    request_id: str = Field(min_length=1)

    _scheduled_time_is_timezone_aware = field_validator("scheduled_time")(_require_timezone)

    @model_validator(mode="after")
    def action_timing_is_valid(self) -> "ActionProposal":
        if self.action_type is ActionType.DELAY_UNTIL and self.scheduled_time is None:
            raise ValueError("delay_until actions require scheduled_time")
        return self


class RailCheck(ContractModel):
    name: str = Field(min_length=1)
    passed: bool
    reason: str | None = None


class TrustComponentScores(ContractModel):
    ragas_faithfulness: float | None = Field(default=None, ge=0, le=1)
    ragas_context_precision: float | None = Field(default=None, ge=0, le=1)
    nemo_rails: float | None = Field(default=None, ge=0, le=1)
    data_freshness: float | None = Field(default=None, ge=0, le=1)
    execution_feasibility: float | None = Field(default=None, ge=0, le=1)

    def missing(self) -> list[str]:
        return [
            field_name
            for field_name, value in self.model_dump().items()
            if value is None
        ]


class TrustWeights(ContractModel):
    ragas_faithfulness: float = Field(ge=0, le=1)
    ragas_context_precision: float = Field(ge=0, le=1)
    nemo_rails: float = Field(ge=0, le=1)
    data_freshness: float = Field(ge=0, le=1)
    execution_feasibility: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def weights_sum_to_one(self) -> "TrustWeights":
        total = sum(self.model_dump().values())
        if abs(total - 1.0) > 1e-9:
            raise ValueError("trust weights must sum to one")
        return self


class TrustPolicy(ContractModel):
    threshold: float = Field(ge=0, le=1)
    weights: TrustWeights


class TrustReport(ContractModel):
    components: TrustComponentScores
    rail_checks: list[RailCheck] = Field(default_factory=list)
    weighted_score: float | None = Field(default=None, ge=0, le=1)
    threshold: float = Field(ge=0, le=1)
    passed: bool
    failure_reason: str | None = None


class ExecutionReceipt(ContractModel):
    run_id: str = Field(min_length=1)
    cycle_id: str = Field(min_length=1)
    mode: DecisionMode
    final_action: ActionProposal
    fallback_triggered: bool
    cluster_name: str | None = None
    job_name: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    benchmark_artifact_paths: list[str] = Field(default_factory=list)
    actual_p95_latency_ms: float | None = Field(default=None, gt=0)
    actual_error_rate: float | None = Field(default=None, ge=0, le=1)
    slo_passed: bool | None = None
    execution_failure_reason: str | None = None

    _started_at_is_timezone_aware = field_validator("started_at")(_require_timezone)
    _completed_at_is_timezone_aware = field_validator("completed_at")(_require_timezone)

    @model_validator(mode="after")
    def execution_times_are_ordered(self) -> "ExecutionReceipt":
        if (
            self.started_at is not None
            and self.completed_at is not None
            and self.completed_at < self.started_at
        ):
            raise ValueError("completed_at must be at or after started_at")
        return self


class ScheduleCandidate(ContractModel):
    target_region: str = Field(min_length=1)
    scheduled_time: datetime
    carbon_intensity_gco2eq_per_kwh: float = Field(ge=0)
    expected_p95_latency_ms: float = Field(gt=0)
    region_available: bool = True
    reset_ready: bool = True

    _scheduled_time_is_timezone_aware = field_validator("scheduled_time")(_require_timezone)


class SchedulingRequest(ContractModel):
    workload_profile: str = Field(min_length=1)
    default_region: str = Field(min_length=1)
    earliest_start: datetime
    deadline: datetime
    p95_slo_limit_ms: float = Field(gt=0)
    candidates: list[ScheduleCandidate] = Field(min_length=1)

    _earliest_is_timezone_aware = field_validator("earliest_start")(_require_timezone)
    _deadline_is_timezone_aware = field_validator("deadline")(_require_timezone)

    @model_validator(mode="after")
    def scheduling_window_is_valid(self) -> "SchedulingRequest":
        if self.deadline < self.earliest_start:
            raise ValueError("deadline must be at or after earliest_start")
        return self


class ScheduleResult(ContractModel):
    status: Literal["selected", "infeasible", "solver_unavailable"]
    action_type: ActionType | None = None
    target_region: str | None = None
    scheduled_time: datetime | None = None
    expected_p95_latency_ms: float | None = Field(default=None, gt=0)
    carbon_intensity_gco2eq_per_kwh: float | None = Field(default=None, ge=0)
    objective_value: float | None = Field(default=None, ge=0)
    solver_backend: str
    reason: str | None = None

    _scheduled_time_is_timezone_aware = field_validator("scheduled_time")(_require_timezone)

    @model_validator(mode="after")
    def selected_result_has_an_action(self) -> "ScheduleResult":
        selection_fields = (
            self.action_type,
            self.target_region,
            self.scheduled_time,
            self.expected_p95_latency_ms,
            self.carbon_intensity_gco2eq_per_kwh,
        )
        if self.status == "selected" and any(value is None for value in selection_fields):
            raise ValueError("selected schedule results require complete action fields")
        if self.status != "selected" and any(value is not None for value in selection_fields):
            raise ValueError("non-selected schedule results must not contain action fields")
        return self


class CarbonApiRecord(ContractModel):
    query_mode: Literal["latest", "forecast", "historical"]
    provider_region: str = Field(min_length=1)
    requested_at: datetime
    received_at: datetime
    http_status: int | None = Field(default=None, ge=100, le=599)
    payload: dict[str, Any] | list[Any] | None = None
    error: str | None = None

    _requested_at_is_timezone_aware = field_validator("requested_at")(_require_timezone)
    _received_at_is_timezone_aware = field_validator("received_at")(_require_timezone)
