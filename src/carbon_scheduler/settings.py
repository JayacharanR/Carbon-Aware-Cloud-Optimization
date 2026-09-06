"""Experiment configuration parsing and validation."""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from pathlib import Path
import re
from typing import Any

import yaml
from pydantic import Field, ValidationError, field_validator, model_validator

from .schemas import ContractModel, DecisionMode, TrustPolicy


class AzureSettings(ContractModel):
    resource_group: str = Field(min_length=1)
    primary_region: str = Field(min_length=1)
    secondary_region: str = Field(min_length=1)
    primary_cluster: str = Field(min_length=1)
    secondary_cluster: str = Field(min_length=1)


class CarbonSettings(ContractModel):
    api_base_url: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    primary_provider_region: str = Field(min_length=1)
    secondary_provider_region: str = Field(min_length=1)
    forecast_horizon_hours: int = Field(gt=0)
    cache_directory: str = Field(min_length=1)
    cache_max_age_seconds: int = Field(gt=0)
    replay_trace_path: str | None = None

    @field_validator("provider")
    @classmethod
    def provider_is_azure(cls, value: str) -> str:
        if value.lower() != "azure":
            raise ValueError("carbon.provider must be 'azure' for provider-region queries")
        return value.lower()


class WorkloadSettings(ContractModel):
    namespace: str = Field(min_length=1)
    frontend_service: str = Field(min_length=1)
    benchmark_script: str = Field(min_length=1)
    profile_id: str = Field(min_length=1)
    earliest_start: str = Field(min_length=1)
    deadline: str = Field(min_length=1)
    reset_before_session: bool = True

    @field_validator("earliest_start", "deadline", mode="before")
    @classmethod
    def yaml_timestamp_to_string(cls, value: Any) -> Any:
        # PyYAML resolves an unquoted ISO timestamp to ``datetime`` before
        # Pydantic sees it.  Normalize that equivalent representation so an
        # operator does not have to know this parser detail; the next validator
        # still enforces an explicit timezone.
        if isinstance(value, datetime):
            return value.isoformat()
        return value

    @field_validator("earliest_start", "deadline")
    @classmethod
    def timestamp_is_timezone_aware(cls, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError) as error:
            raise ValueError("workload timestamps must be valid ISO-8601 values") from error
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("workload timestamps must include a timezone")
        return value

    @model_validator(mode="after")
    def scheduling_window_is_ordered(self) -> "WorkloadSettings":
        start = datetime.fromisoformat(self.earliest_start.replace("Z", "+00:00"))
        deadline = datetime.fromisoformat(self.deadline.replace("Z", "+00:00"))
        if deadline < start:
            raise ValueError("workload deadline must be at or after earliest_start")
        return self


class SloSettings(ContractModel):
    profile_p95_limit_ms: float = Field(gt=0)
    profile_error_rate_limit: float = Field(ge=0, le=1)


class MilpSettings(ContractModel):
    slot_minutes: int = Field(gt=0)
    allowed_regions: list[str] = Field(min_length=1)
    latency_catalog_path: str = Field(min_length=1)

    @field_validator("allowed_regions")
    @classmethod
    def regions_are_unique(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("milp.allowed_regions must not contain duplicates")
        return value


class OllamaSettings(ContractModel):
    base_url: str = Field(min_length=1)
    model: str = Field(min_length=1)
    request_timeout_seconds: int = Field(gt=0)
    retry_count: int = Field(ge=0)


class ExperimentSettings(ContractModel):
    modes: list[str] = Field(min_length=1)
    static_reference_region: str = Field(min_length=1)
    random_seed: int

    @field_validator("modes")
    @classmethod
    def modes_are_supported_and_unique(cls, value: list[str]) -> list[str]:
        supported = {mode.value for mode in DecisionMode}
        unknown = sorted(set(value) - supported)
        if unknown:
            raise ValueError("experiments.modes contains unsupported mode(s): " + ", ".join(unknown))
        if len(set(value)) != len(value):
            raise ValueError("experiments.modes must not contain duplicates")
        required = {
            DecisionMode.LLM_ONLY.value,
            DecisionMode.MILP_ONLY.value,
            DecisionMode.HYBRID.value,
        }
        missing = sorted(required - set(value))
        if missing:
            raise ValueError("experiments.modes is missing required comparison mode(s): " + ", ".join(missing))
        return value


class ExperimentConfig(ContractModel):
    azure: AzureSettings
    carbon: CarbonSettings
    workload: WorkloadSettings
    slo: SloSettings
    trust: TrustPolicy
    milp: MilpSettings
    ollama: OllamaSettings
    experiments: ExperimentSettings

    @model_validator(mode="after")
    def enforce_two_region_scope(self) -> "ExperimentConfig":
        regions = [self.azure.primary_region, self.azure.secondary_region]
        if regions[0] == regions[1]:
            raise ValueError("primary and secondary Azure regions must be different")
        provider_regions = [
            self.carbon.primary_provider_region,
            self.carbon.secondary_provider_region,
        ]
        if provider_regions[0] == provider_regions[1]:
            raise ValueError(
                "primary and secondary Electricity Maps provider regions must be different"
            )
        if set(self.milp.allowed_regions) != set(regions):
            raise ValueError(
                "milp.allowed_regions must contain exactly the primary and secondary Azure regions"
            )
        if self.experiments.static_reference_region not in regions:
            raise ValueError("experiments.static_reference_region must be one of the two Azure regions")
        return self


class ConfigurationError(ValueError):
    """Raised when a configuration file cannot safely drive an experiment."""


def load_experiment_config(path: str | Path) -> ExperimentConfig:
    """Load a completed experiment configuration; drafts with empty fields fail."""

    config_path = Path(path)
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ConfigurationError(f"cannot read configuration: {config_path}") from error
    except yaml.YAMLError as error:
        raise ConfigurationError(f"invalid YAML in configuration: {config_path}") from error

    if not isinstance(raw, dict):
        raise ConfigurationError("experiment configuration must be a YAML mapping")
    placeholders = _placeholder_paths(raw)
    if placeholders:
        raise ConfigurationError(
            "experiment configuration contains placeholder values at: "
            + ", ".join(placeholders)
        )
    try:
        return ExperimentConfig.model_validate(raw)
    except ValidationError as error:
        raise ConfigurationError(f"invalid experiment configuration: {error}") from error


def configuration_sha256(path: str | Path) -> str:
    """Return the content hash written into a run manifest."""

    content = Path(path).read_bytes()
    return sha256(content).hexdigest()


def config_to_dict(config: ExperimentConfig) -> dict[str, Any]:
    """Return a JSON-compatible configuration record for logging."""

    return config.model_dump(mode="json")


def _placeholder_paths(value: Any, path: str = "") -> list[str]:
    """Find explicit template markers before Pydantic coerces/validates values."""

    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            found.extend(_placeholder_paths(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_placeholder_paths(child, f"{path}[{index}]"))
    elif isinstance(value, str):
        stripped = value.strip()
        if "REPLACE_ME" in stripped.upper() or re.fullmatch(r"<[^>]+>", stripped):
            found.append(path)
    return found
