"""Structured Ollama proposal generation.

This module accepts only JSON that validates against ``ActionProposal``.  It
does not turn an unavailable model into a made-up scheduling decision; callers
receive a precise failure that the hybrid workflow can send to MILP.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import uuid4

import requests
from pydantic import ValidationError

from .schemas import ActionProposal, DecisionInput


PROMPT_VERSION = "structured-scheduler-v1"


class AgentError(RuntimeError):
    """Base error for an agent request that cannot yield a usable proposal."""


class AgentUnavailableError(AgentError):
    """The Ollama endpoint or tunnel could not supply a response."""


class AgentResponseError(AgentError):
    """The model returned output that is not a permitted proposal."""


class ProposalAgent(Protocol):
    def propose(
        self,
        decision_input: DecisionInput,
        *,
        graph_context: dict[str, Any],
        retrieval_context: str,
    ) -> ActionProposal: ...


@dataclass(frozen=True, slots=True)
class AgentCallRecord:
    """Metadata about a model call, suitable for a controller event record."""

    request_id: str
    endpoint: str
    model: str
    attempts: int
    started_at: datetime
    completed_at: datetime | None
    error: str | None = None


class OllamaStructuredAgent:
    """Call a local/remote Ollama endpoint and validate its structured result."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        timeout_seconds: float,
        retry_count: int = 0,
        session: requests.Session | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if retry_count < 0:
            raise ValueError("retry_count must not be negative")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.retry_count = retry_count
        self.session = session or requests.Session()
        self.last_call: AgentCallRecord | None = None

    def propose(
        self,
        decision_input: DecisionInput,
        *,
        graph_context: dict[str, Any],
        retrieval_context: str,
    ) -> ActionProposal:
        request_id = str(uuid4())
        started_at = datetime.now().astimezone()
        endpoint = f"{self.base_url}/api/chat"
        payload = {
            "model": self.model,
            "stream": False,
            "format": ActionProposal.model_json_schema(),
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a constrained benchmark-session scheduler. "
                        "Return JSON only, exactly matching the supplied schema. "
                        "Do not create regions, services, evidence IDs, or times that are absent "
                        "from the supplied policy."
                    ),
                },
                {
                    "role": "user",
                    "content": build_scheduler_prompt(
                        decision_input,
                        graph_context=graph_context,
                        retrieval_context=retrieval_context,
                        request_id=request_id,
                        model_name=self.model,
                    ),
                },
            ],
        }

        errors: list[str] = []
        network_failure = False
        for attempt in range(1, self.retry_count + 2):
            try:
                response = self.session.post(endpoint, json=payload, timeout=self.timeout_seconds)
                response.raise_for_status()
                raw = response.json()
                content = _ollama_content(raw)
                proposal = parse_action_proposal(content)
                _validate_response_identity(proposal, request_id=request_id, model_name=self.model)
                validate_proposal_against_input(proposal, decision_input, graph_context)
                self.last_call = AgentCallRecord(
                    request_id=request_id,
                    endpoint=endpoint,
                    model=self.model,
                    attempts=attempt,
                    started_at=started_at,
                    completed_at=datetime.now().astimezone(),
                )
                return proposal
            except requests.RequestException as error:
                network_failure = True
                errors.append(str(error))
                if attempt <= self.retry_count:
                    time.sleep(min(0.25 * attempt, 1.0))
            except AgentError as error:
                # Keep malformed/non-object model responses in the response
                # error class, while still honoring the configured bounded
                # retry count and preserving a call record.
                network_failure = network_failure or isinstance(error, AgentUnavailableError)
                errors.append(str(error))
                if attempt <= self.retry_count:
                    time.sleep(min(0.25 * attempt, 1.0))
            except (ValueError, TypeError, json.JSONDecodeError) as error:
                errors.append(str(error))
                if attempt <= self.retry_count:
                    # Small bounded backoff.  The controller records tunnel failures
                    # separately through the resulting AgentUnavailableError.
                    time.sleep(min(0.25 * attempt, 1.0))

        message = "; ".join(error for error in errors if error) or "Ollama returned no usable response"
        self.last_call = AgentCallRecord(
            request_id=request_id,
            endpoint=endpoint,
            model=self.model,
            attempts=self.retry_count + 1,
            started_at=started_at,
            completed_at=datetime.now().astimezone(),
            error=message,
        )
        if network_failure:
            raise AgentUnavailableError(message)
        raise AgentResponseError(message)


class DemoProposalAgent:
    """Explicitly non-experimental deterministic agent for offline plumbing demos.

    It must only be used through the ``--demo`` CLI switch.  Its output is
    tagged with ``demo-agent`` so it cannot be mistaken for an Ollama result.
    """

    def __init__(self, *, expected_p95_latency_ms: float) -> None:
        if expected_p95_latency_ms <= 0:
            raise ValueError("expected_p95_latency_ms must be positive")
        self.expected_p95_latency_ms = expected_p95_latency_ms

    def propose(
        self,
        decision_input: DecisionInput,
        *,
        graph_context: dict[str, Any],
        retrieval_context: str,
    ) -> ActionProposal:
        del retrieval_context
        # This agent is intentionally simple: it chooses the lowest current
        # input value.  It is only a fake integration endpoint, never evidence.
        target_region = min(
            decision_input.candidate_regions,
            key=lambda region: decision_input.carbon_snapshot.regions[
                region
            ].current.carbon_intensity_gco2eq_per_kwh,
        )
        target_service = str(graph_context.get("service") or decision_input.dependency_context.target_service)
        return ActionProposal(
            action_type=(
                "run_now"
                if target_region == decision_input.candidate_regions[0]
                else "run_in_region"
            ),
            target_region=target_region,
            target_service=target_service,
            scheduled_time=None,
            workload_profile=decision_input.workload_profile,
            expected_p95_latency_ms=self.expected_p95_latency_ms,
            self_reported_confidence=1.0,
            evidence_ids=list(decision_input.retrieval_context_ids),
            rationale="Offline demo proposal derived from the supplied replay input.",
            model_name="demo-agent",
            prompt_version=PROMPT_VERSION,
            request_id=f"demo-{decision_input.cycle_id}",
        )


def build_scheduler_prompt(
    decision_input: DecisionInput,
    *,
    graph_context: dict[str, Any],
    retrieval_context: str,
    request_id: str,
    model_name: str,
) -> str:
    """Render the bounded policy record presented to the LLM."""

    carbon = {
        region: {
            "current_carbon_intensity_gco2eq_per_kwh": data.current.carbon_intensity_gco2eq_per_kwh,
            "current_timestamp": data.current.timestamp.isoformat(),
            "forecast": [point.model_dump(mode="json") for point in data.forecast],
            "source": data.source.value,
        }
        for region, data in decision_input.carbon_snapshot.regions.items()
    }
    policy = {
        "request_id": request_id,
        "required_model_name": model_name,
        "required_prompt_version": PROMPT_VERSION,
        "workload_profile": decision_input.workload_profile,
        "allowed_action_types": ["run_now", "run_in_region", "delay_until"],
        "candidate_regions": decision_input.candidate_regions,
        "allowed_services": graph_context.get("allowed_services", [decision_input.dependency_context.target_service]),
        "earliest_start": decision_input.earliest_start.isoformat(),
        "deadline": decision_input.deadline.isoformat(),
        "p95_slo_limit_ms": decision_input.p95_slo_limit_ms,
        "cluster_available": decision_input.cluster_available,
        "carbon": carbon,
        "dependency_context": graph_context,
        "retrieved_context": retrieval_context,
        "required_json_schema": ActionProposal.model_json_schema(),
    }
    return json.dumps(policy, sort_keys=True, default=str)


def parse_action_proposal(value: str | dict[str, Any]) -> ActionProposal:
    """Parse strict model output without attempting free-text repair."""

    try:
        payload = json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError as error:
        raise AgentResponseError("model response was not JSON") from error
    if not isinstance(payload, dict):
        raise AgentResponseError("model response must be a JSON object")
    try:
        return ActionProposal.model_validate(payload)
    except ValidationError as error:
        raise AgentResponseError(f"model response did not match ActionProposal: {error}") from error


def validate_proposal_against_input(
    proposal: ActionProposal,
    decision_input: DecisionInput,
    graph_context: dict[str, Any],
) -> None:
    """Reject unknown or out-of-window agent actions before execution."""

    if proposal.target_region not in decision_input.candidate_regions:
        raise AgentResponseError("proposal references a region outside candidate_regions")
    allowed_services = set(graph_context.get("allowed_services") or [decision_input.dependency_context.target_service])
    if proposal.target_service not in allowed_services:
        raise AgentResponseError("proposal references a service absent from the manifest graph")
    scheduled_time = proposal.scheduled_time or decision_input.earliest_start
    if scheduled_time < decision_input.earliest_start or scheduled_time > decision_input.deadline:
        raise AgentResponseError("proposal scheduled_time is outside the decision window")
    if proposal.expected_p95_latency_ms > decision_input.p95_slo_limit_ms:
        raise AgentResponseError("proposal expected p95 latency exceeds the configured SLO")


def _validate_response_identity(proposal: ActionProposal, *, request_id: str, model_name: str) -> None:
    if proposal.request_id != request_id:
        raise AgentResponseError("model response request_id does not match the issued request")
    if proposal.model_name != model_name:
        raise AgentResponseError("model response model_name does not match the configured model")
    if proposal.prompt_version != PROMPT_VERSION:
        raise AgentResponseError("model response prompt_version is unsupported")


def _ollama_content(response: dict[str, Any]) -> str:
    if not isinstance(response, dict):
        raise AgentResponseError("Ollama response must be a JSON object")
    message = response.get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        return message["content"]
    if isinstance(response.get("response"), str):
        return str(response["response"])
    raise AgentResponseError("Ollama response did not include message.content")
