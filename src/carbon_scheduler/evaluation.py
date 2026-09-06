"""Online RAGAS evaluation adapters for the hybrid trust gate.

RAGAS is intentionally an optional import.  A missing or failed evaluator is
reported to the caller, which must fail the hybrid gate and use the MILP
fallback; this module never creates substitute metric values.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import types
from dataclasses import dataclass
from typing import Any, Iterable, Protocol

from .schemas import ActionProposal, DecisionInput


class TrustEvaluationError(RuntimeError):
    """The configured RAGAS evaluator could not produce a usable score."""


@dataclass(frozen=True, slots=True)
class RagasScores:
    """The two online RAGAS scores used by the configured trust policy."""

    faithfulness: float
    context_precision: float


class TrustEvaluator(Protocol):
    """Minimal evaluator contract, which permits test-only injected scorers."""

    def evaluate(
        self,
        *,
        decision_input: DecisionInput,
        proposal: ActionProposal,
        retrieval_documents: Iterable[str],
        canonical_policy: str,
    ) -> RagasScores: ...


def build_canonical_policy_record(
    decision_input: DecisionInput,
    graph_context: dict[str, Any],
) -> str:
    """Render the deterministic reference used for context-precision scoring.

    It contains frozen decision inputs only.  It is not an optimizer answer and
    does not leak a MILP choice into the LLM evaluation.
    """

    carbon = {
        region: {
            "current_carbon_intensity_gco2eq_per_kwh": data.current.carbon_intensity_gco2eq_per_kwh,
            "current_timestamp": data.current.timestamp.isoformat(),
            "forecast": [point.model_dump(mode="json") for point in data.forecast],
            "source": data.source.value,
        }
        for region, data in sorted(decision_input.carbon_snapshot.regions.items())
    }
    policy = {
        "allowed_action_types": ["run_now", "run_in_region", "delay_until"],
        "candidate_regions": decision_input.candidate_regions,
        "allowed_services": graph_context.get(
            "allowed_services", [decision_input.dependency_context.target_service]
        ),
        "workload_profile": decision_input.workload_profile,
        "earliest_start": decision_input.earliest_start.isoformat(),
        "deadline": decision_input.deadline.isoformat(),
        "p95_slo_limit_ms": decision_input.p95_slo_limit_ms,
        "error_rate_limit": decision_input.error_rate_limit,
        "cluster_available": decision_input.cluster_available,
        "carbon": carbon,
    }
    return json.dumps(policy, sort_keys=True, separators=(",", ":"))


class RagasOllamaEvaluator:
    """Run RAGAS faithfulness and context precision through Ollama's OAI API.

    The implementation follows the RAGAS supported OpenAI-compatible client
    path.  Ollama itself remains the evaluator model; no external paid model is
    introduced.  The configured Ollama endpoint must expose its `/v1` API.
    """

    def __init__(self, *, base_url: str, model: str, timeout_seconds: float) -> None:
        if not base_url.strip():
            raise ValueError("base_url must not be empty")
        if not model.strip():
            raise ValueError("model must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.base_url = _openai_compatible_base_url(base_url)
        self.model = model
        self.timeout_seconds = timeout_seconds

    def evaluate(
        self,
        *,
        decision_input: DecisionInput,
        proposal: ActionProposal,
        retrieval_documents: Iterable[str],
        canonical_policy: str,
    ) -> RagasScores:
        documents = [document for document in retrieval_documents if document.strip()]
        if not documents:
            raise TrustEvaluationError("RAGAS requires at least one retrieved context document")
        response = json.dumps(
            {
                "action_type": proposal.action_type.value,
                "target_region": proposal.target_region,
                "target_service": proposal.target_service,
                "scheduled_time": proposal.scheduled_time.isoformat()
                if proposal.scheduled_time
                else None,
                "expected_p95_latency_ms": proposal.expected_p95_latency_ms,
                "rationale": proposal.rationale,
                "evidence_ids": proposal.evidence_ids,
            },
            sort_keys=True,
        )
        question = (
            "Choose a permitted benchmark-session action using the supplied "
            "carbon, service, timing, and SLO policy."
        )
        try:
            result = _run_awaitable(
                self._score_async(
                    user_input=question,
                    response=response,
                    retrieved_contexts=documents,
                    reference=canonical_policy,
                )
            )
        except TrustEvaluationError:
            raise
        except Exception as error:  # optional dependency or remote evaluator failure
            raise TrustEvaluationError(f"RAGAS evaluation failed: {error}") from error
        return result

    async def _score_async(
        self,
        *,
        user_input: str,
        response: str,
        retrieved_contexts: list[str],
        reference: str,
    ) -> RagasScores:
        try:
            from openai import AsyncOpenAI
            llm_factory, ContextPrecision, Faithfulness = _import_ragas_dependencies()
        except ImportError as error:
            raise TrustEvaluationError(
                "RAGAS evaluation dependencies are not installed; install the agent extra"
            ) from error

        client = AsyncOpenAI(
            api_key="ollama",
            base_url=self.base_url,
            timeout=self.timeout_seconds,
        )
        try:
            evaluator_llm = llm_factory(self.model, provider="openai", client=client)
            faithfulness = Faithfulness(llm=evaluator_llm)
            context_precision = ContextPrecision(llm=evaluator_llm)
            faithfulness_result = await faithfulness.ascore(
                user_input=user_input,
                response=response,
                retrieved_contexts=retrieved_contexts,
            )
            precision_result = await context_precision.ascore(
                user_input=user_input,
                reference=reference,
                retrieved_contexts=retrieved_contexts,
            )
            return RagasScores(
                faithfulness=_score_value(faithfulness_result),
                context_precision=_score_value(precision_result),
            )
        finally:
            await client.close()


def _import_ragas_dependencies() -> tuple[Any, Any, Any]:
    """Load RAGAS metrics across current LangChain community package layouts.

    RAGAS 0.4.x still imports ``langchain_community.chat_models.vertexai``
    unconditionally, although recent ``langchain-community`` releases moved
    that integration to a separate package.  The scheduler uses RAGAS with an
    OpenAI-compatible Ollama client, so the Vertex AI class is never
    instantiated.  Registering a marker class only for that unused import
    keeps the supported Ollama path working without adding Google credentials
    or an unrelated provider dependency.  Any other import error is surfaced
    to the trust gate, which then fails closed to MILP.
    """

    try:
        from ragas.llms import llm_factory
        from ragas.metrics.collections import ContextPrecision, Faithfulness
    except ModuleNotFoundError as error:
        if error.name != "langchain_community.chat_models.vertexai":
            raise
        module_name = error.name
        compatibility_module = types.ModuleType(module_name)
        compatibility_module.ChatVertexAI = type("ChatVertexAI", (), {})
        sys.modules[module_name] = compatibility_module
        from ragas.llms import llm_factory
        from ragas.metrics.collections import ContextPrecision, Faithfulness
    return llm_factory, ContextPrecision, Faithfulness


def _score_value(value: Any) -> float:
    raw = getattr(value, "value", value)
    try:
        score = float(raw)
    except (TypeError, ValueError) as error:
        raise TrustEvaluationError(f"RAGAS returned a nonnumeric score: {raw!r}") from error
    if not 0 <= score <= 1:
        raise TrustEvaluationError(f"RAGAS returned an out-of-range score: {score}")
    return score


def _openai_compatible_base_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/v1"):
        return normalized
    if normalized.endswith("/api"):
        normalized = normalized[: -len("/api")]
    return f"{normalized}/v1"


def _run_awaitable(awaitable: Any) -> Any:
    """Run an async RAGAS call from sync CLI or controller code safely."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)

    # A controller may eventually call this from an async host.  Avoid nested
    # event loops by using a short-lived worker thread and preserve exceptions.
    result: list[Any] = []
    errors: list[BaseException] = []

    def runner() -> None:
        try:
            result.append(asyncio.run(awaitable))
        except BaseException as error:  # noqa: BLE001 - re-raised in caller thread
            errors.append(error)

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join()
    if errors:
        raise errors[0]
    return result[0]
