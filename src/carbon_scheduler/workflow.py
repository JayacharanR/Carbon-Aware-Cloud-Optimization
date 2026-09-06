"""Decision flow for LLM-only, MILP-only, and trust-gated hybrid modes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, TypedDict

import yaml

from .agent import AgentError, AgentResponseError, AgentUnavailableError, ProposalAgent
from .evaluation import RagasScores, TrustEvaluationError, TrustEvaluator, build_canonical_policy_record
from .executor import BenchmarkExecutor, ExecutionError
from .milp import solve_schedule
from .schemas import (
    ActionProposal,
    ActionType,
    CarbonDataSource,
    DecisionInput,
    DecisionMode,
    ExecutionReceipt,
    ScheduleCandidate,
    ScheduleResult,
    SchedulingRequest,
    TrustComponentScores,
    TrustPolicy,
    TrustReport,
)
from .trust import evaluate_trust, validate_proposal
from .telemetry import NoopTelemetry, Telemetry


class WorkflowError(RuntimeError):
    """A workflow prerequisite was missing or inconsistent."""


@dataclass(frozen=True, slots=True)
class CycleResult:
    """Complete, serialisable outcome of one scheduler decision cycle."""

    mode: DecisionMode
    decision_input: DecisionInput
    proposal: ActionProposal | None
    trust_report: TrustReport | None
    schedule_result: ScheduleResult | None
    execution_receipt: ExecutionReceipt | None
    outcome: str
    failure_reason: str | None = None

    def to_record(self) -> dict[str, Any]:
        """Convert to a JSON-safe event record without dropping failed states."""

        return {
            "record_type": "scheduler_cycle",
            "mode": self.mode.value,
            "outcome": self.outcome,
            "failure_reason": self.failure_reason,
            "decision_input": self.decision_input.model_dump(mode="json"),
            "proposal": self.proposal.model_dump(mode="json") if self.proposal else None,
            "trust_report": self.trust_report.model_dump(mode="json") if self.trust_report else None,
            "schedule_result": self.schedule_result.model_dump(mode="json")
            if self.schedule_result
            else None,
            "execution_receipt": self.execution_receipt.model_dump(mode="json")
            if self.execution_receipt
            else None,
        }


class _LangGraphState(TypedDict, total=False):
    """Internal state passed between the optional LangGraph nodes."""

    mode: DecisionMode
    decision_input: DecisionInput
    graph_context: dict[str, Any]
    documents: list[str]
    proposal: ActionProposal | None
    rails: list[Any]
    trust_report: TrustReport | None
    schedule_result: ScheduleResult | None
    fallback_triggered: bool
    terminal_outcome: str | None
    terminal_reason: str | None
    execution_receipt: ExecutionReceipt | None
    result: CycleResult


class SchedulerWorkflow:
    """The bounded controller flow described in the project plan.

    The workflow chooses a fresh benchmark session; it has no operation for
    moving live services or state between clusters.
    """

    def __init__(
        self,
        *,
        agent: ProposalAgent | None,
        trust_evaluator: TrustEvaluator | None,
        executor: BenchmarkExecutor,
        trust_policy: TrustPolicy,
        default_region: str,
        latency_catalog: Mapping[str, Mapping[str, float]],
        slot_minutes: int | None = None,
        orchestration_backend: str = "direct",
        telemetry: Telemetry | None = None,
    ) -> None:
        if not default_region:
            raise ValueError("default_region must not be empty")
        self.agent = agent
        self.trust_evaluator = trust_evaluator
        self.executor = executor
        self.trust_policy = trust_policy
        self.default_region = default_region
        if slot_minutes is not None and slot_minutes <= 0:
            raise ValueError("slot_minutes must be positive when supplied")
        self.slot_minutes = slot_minutes
        if orchestration_backend not in {"direct", "langgraph"}:
            raise ValueError("orchestration_backend must be 'direct' or 'langgraph'")
        self.orchestration_backend = orchestration_backend
        self.latency_catalog = {
            profile: {region: float(value) for region, value in values.items()}
            for profile, values in latency_catalog.items()
        }
        self.telemetry = telemetry or NoopTelemetry()

    def run_cycle(
        self,
        *,
        mode: DecisionMode,
        decision_input: DecisionInput,
        graph_context: Mapping[str, Any],
        retrieval_documents: Iterable[str],
    ) -> CycleResult:
        """Trace and run one complete decision cycle."""

        with self.telemetry.span(
            "scheduler.cycle",
            {
                "scheduler.run_id": decision_input.run_id,
                "scheduler.cycle_id": decision_input.cycle_id,
                "scheduler.mode": mode.value,
            },
        ):
            if self.orchestration_backend == "langgraph":
                return self._run_cycle_langgraph(
                    mode=mode,
                    decision_input=decision_input,
                    graph_context=graph_context,
                    retrieval_documents=retrieval_documents,
                )
            return self._run_cycle(
                mode=mode,
                decision_input=decision_input,
                graph_context=graph_context,
                retrieval_documents=retrieval_documents,
            )

    def _run_cycle_langgraph(
        self,
        *,
        mode: DecisionMode,
        decision_input: DecisionInput,
        graph_context: Mapping[str, Any],
        retrieval_documents: Iterable[str],
    ) -> CycleResult:
        """Run the same bounded flow through the optional LangGraph runtime.

        The direct implementation remains the dependency-light default.  The
        LangGraph path uses six explicit nodes so the deployed controller has a
        visible context/proposal/evaluation/choice/execution/record boundary,
        while both paths share the same contracts and safety helpers.
        """

        try:
            from langgraph.graph import END, START, StateGraph
        except ImportError as error:  # pragma: no cover - optional deployment dependency
            raise WorkflowError(
                "LangGraph orchestration was requested but langgraph is not installed"
            ) from error

        graph = StateGraph(_LangGraphState)
        graph.add_node("collect_context", self._lg_collect_context)
        graph.add_node("propose_action", self._lg_propose_action)
        graph.add_node("evaluate_trust", self._lg_evaluate_trust)
        graph.add_node("choose_final_action", self._lg_choose_final_action)
        graph.add_node("execute", self._lg_execute)
        graph.add_node("record_result", self._lg_record_result)
        graph.add_edge(START, "collect_context")
        graph.add_edge("collect_context", "propose_action")
        graph.add_edge("propose_action", "evaluate_trust")
        graph.add_edge("evaluate_trust", "choose_final_action")
        graph.add_edge("choose_final_action", "execute")
        graph.add_edge("execute", "record_result")
        graph.add_edge("record_result", END)
        application = graph.compile()
        output = application.invoke(
            {
                "mode": mode,
                "decision_input": decision_input,
                "graph_context": dict(graph_context),
                "documents": list(retrieval_documents),
            }
        )
        result = output.get("result")
        if not isinstance(result, CycleResult):
            raise WorkflowError("LangGraph run did not produce a CycleResult")
        return result

    def _lg_collect_context(self, state: _LangGraphState) -> dict[str, Any]:
        graph_context = dict(state.get("graph_context") or {})
        documents = [
            document
            for document in state.get("documents", [])
            if isinstance(document, str) and document.strip()
        ]
        return {"graph_context": graph_context, "documents": documents}

    def _lg_propose_action(self, state: _LangGraphState) -> dict[str, Any]:
        mode = state["mode"]
        decision_input = state["decision_input"]
        if mode in {DecisionMode.MILP_ONLY, DecisionMode.STATIC_REFERENCE}:
            return {}
        if self.agent is None:
            reason = "no LLM proposal agent is configured"
            if mode is DecisionMode.HYBRID:
                return {"terminal_reason": reason}
            return {"terminal_outcome": "proposal_unavailable", "terminal_reason": reason}
        try:
            with self.telemetry.span("scheduler.agent.propose"):
                proposal = self.agent.propose(
                    decision_input,
                    graph_context=dict(state.get("graph_context") or {}),
                    retrieval_context="\n\n".join(state.get("documents", [])),
                )
        except AgentError as error:
            reason = _agent_failure_reason(error)
            if mode is DecisionMode.HYBRID:
                return {"terminal_reason": reason}
            return {"terminal_outcome": "proposal_unavailable", "terminal_reason": reason}
        except Exception as error:  # fail closed in hybrid, preserve LLM-only no-action state
            reason = f"llm_agent_error: unexpected LLM proposal failure: {error}"
            if mode is DecisionMode.HYBRID:
                return {"terminal_reason": reason}
            return {"terminal_outcome": "proposal_unavailable", "terminal_reason": reason}

        rails = validate_proposal(
            proposal,
            allowed_regions=decision_input.candidate_regions,
            allowed_services=dict(state.get("graph_context") or {}).get(
                "allowed_services", [decision_input.dependency_context.target_service]
            ),
            earliest_start=decision_input.earliest_start,
            deadline=decision_input.deadline,
            region_available=decision_input.cluster_available,
            p95_slo_limit_ms=decision_input.p95_slo_limit_ms,
        )
        return {"proposal": proposal, "rails": rails}

    def _lg_evaluate_trust(self, state: _LangGraphState) -> dict[str, Any]:
        if state["mode"] is not DecisionMode.HYBRID:
            return {}
        proposal = state.get("proposal")
        if proposal is None:
            reason = state.get("terminal_reason") or "proposal unavailable"
            return {"trust_report": self._failed_report(reason=reason)}
        report = self._evaluate_hybrid_trust(
            decision_input=state["decision_input"],
            proposal=proposal,
            rails=state.get("rails", []),
            documents=state.get("documents", []),
            graph_context=dict(state.get("graph_context") or {}),
        )
        return {"trust_report": report}

    def _lg_choose_final_action(self, state: _LangGraphState) -> dict[str, Any]:
        if state.get("terminal_outcome"):
            return {}
        mode = state["mode"]
        decision_input = state["decision_input"]
        if mode is DecisionMode.MILP_ONLY:
            return self._lg_schedule(state, fallback_triggered=False)
        if mode is DecisionMode.STATIC_REFERENCE:
            proposal, reason = self._build_static_reference_proposal(decision_input)
            if proposal is None:
                return {"terminal_outcome": "infeasible", "terminal_reason": reason}
            return {"proposal": proposal, "fallback_triggered": False}
        if mode is DecisionMode.LLM_ONLY:
            rails = state.get("rails", [])
            if any(not check.passed for check in rails):
                return {
                    "terminal_outcome": "proposal_rejected",
                    "terminal_reason": "failed executor safety rails: "
                    + ", ".join(check.name for check in rails if not check.passed),
                }
            return {"fallback_triggered": False}
        if mode is DecisionMode.HYBRID:
            report = state.get("trust_report")
            if state.get("proposal") is None or report is None or not report.passed:
                return self._lg_schedule(state, fallback_triggered=True)
            return {"fallback_triggered": False}
        raise WorkflowError(f"unsupported decision mode: {mode.value}")

    def _lg_schedule(self, state: _LangGraphState, *, fallback_triggered: bool) -> dict[str, Any]:
        decision_input = state["decision_input"]
        try:
            request = build_scheduling_request(
                decision_input,
                default_region=self.default_region,
                latency_catalog=self.latency_catalog,
                slot_minutes=self.slot_minutes,
            )
            with self.telemetry.span("scheduler.milp.solve"):
                schedule_result = solve_schedule(request)
        except Exception as error:
            return {
                "terminal_outcome": "infeasible",
                "terminal_reason": f"could not build or solve MILP request: {error}",
            }
        if schedule_result.status != "selected":
            return {
                "schedule_result": schedule_result,
                "terminal_outcome": schedule_result.status,
                "terminal_reason": schedule_result.reason,
            }
        return {
            "schedule_result": schedule_result,
            "proposal": proposal_from_schedule(schedule_result, decision_input),
            "fallback_triggered": fallback_triggered,
        }

    def _lg_execute(self, state: _LangGraphState) -> dict[str, Any]:
        if state.get("terminal_outcome") or state.get("proposal") is None:
            return {}
        mode = state["mode"]
        decision_input = state["decision_input"]
        proposal = state["proposal"]
        try:
            with self.telemetry.span("scheduler.executor.execute"):
                receipt = self.executor.execute(
                    action=proposal,
                    mode=mode,
                    run_id=decision_input.run_id,
                    cycle_id=decision_input.cycle_id,
                    carbon_snapshot_id=decision_input.carbon_snapshot.snapshot_id,
                    config_hash=decision_input.config_hash,
                    fallback_triggered=bool(state.get("fallback_triggered", False)),
                )
        except ExecutionError as error:
            return {"terminal_outcome": "execution_failed", "terminal_reason": str(error)}
        except Exception as error:
            return {
                "terminal_outcome": "execution_failed",
                "terminal_reason": f"unexpected executor failure: {error}",
            }
        receipt = _with_slo_status(receipt, decision_input)
        outcome = (
            "dry_run"
            if (receipt.execution_failure_reason or "").startswith("dry-run:")
            else "execution_failed"
            if receipt.execution_failure_reason
            else "executed"
        )
        return {
            "execution_receipt": receipt,
            "terminal_outcome": outcome,
            "terminal_reason": receipt.execution_failure_reason,
        }

    def _lg_record_result(self, state: _LangGraphState) -> dict[str, Any]:
        return {
            "result": CycleResult(
                mode=state["mode"],
                decision_input=state["decision_input"],
                proposal=state.get("proposal"),
                trust_report=state.get("trust_report"),
                schedule_result=state.get("schedule_result"),
                execution_receipt=state.get("execution_receipt"),
                outcome=state.get("terminal_outcome") or "proposal_unavailable",
                failure_reason=state.get("terminal_reason"),
            )
        }

    def _run_cycle(
        self,
        *,
        mode: DecisionMode,
        decision_input: DecisionInput,
        graph_context: Mapping[str, Any],
        retrieval_documents: Iterable[str],
    ) -> CycleResult:
        """Run one decision cycle and preserve the reason for every outcome."""

        graph = dict(graph_context)
        documents = [document for document in retrieval_documents if document.strip()]
        if mode is DecisionMode.MILP_ONLY:
            return self._run_milp(decision_input, fallback_triggered=False)
        if mode is DecisionMode.STATIC_REFERENCE:
            return self._run_static_reference(decision_input)
        if self.agent is None:
            if mode is DecisionMode.HYBRID:
                return self._run_milp(
                    decision_input,
                    fallback_triggered=True,
                    failed_trust_report=self._failed_report(
                        reason="no LLM proposal agent is configured",
                    ),
                )
            return CycleResult(
                mode=mode,
                decision_input=decision_input,
                proposal=None,
                trust_report=None,
                schedule_result=None,
                execution_receipt=None,
                outcome="proposal_unavailable",
                failure_reason="no LLM proposal agent is configured",
            )
        try:
            with self.telemetry.span("scheduler.agent.propose"):
                proposal = self.agent.propose(
                    decision_input,
                    graph_context=graph,
                    retrieval_context="\n\n".join(documents),
                )
        except AgentError as error:
            reason = _agent_failure_reason(error)
            if mode is DecisionMode.HYBRID:
                return self._run_milp(
                    decision_input,
                    fallback_triggered=True,
                    failed_trust_report=self._failed_report(
                        reason=reason,
                    ),
                )
            return CycleResult(
                mode=mode,
                decision_input=decision_input,
                proposal=None,
                trust_report=None,
                schedule_result=None,
                execution_receipt=None,
                outcome="proposal_unavailable",
                failure_reason=reason,
            )
        except Exception as error:  # unexpected integration failure is also fail-closed in hybrid
            reason = f"llm_agent_error: unexpected LLM proposal failure: {error}"
            if mode is DecisionMode.HYBRID:
                return self._run_milp(
                    decision_input,
                    fallback_triggered=True,
                    failed_trust_report=self._failed_report(
                        reason=reason,
                    ),
                )
            return CycleResult(
                mode=mode,
                decision_input=decision_input,
                proposal=None,
                trust_report=None,
                schedule_result=None,
                execution_receipt=None,
                outcome="proposal_unavailable",
                failure_reason=reason,
            )

        rails = validate_proposal(
            proposal,
            allowed_regions=decision_input.candidate_regions,
            allowed_services=graph.get(
                "allowed_services", [decision_input.dependency_context.target_service]
            ),
            earliest_start=decision_input.earliest_start,
            deadline=decision_input.deadline,
            region_available=decision_input.cluster_available,
            p95_slo_limit_ms=decision_input.p95_slo_limit_ms,
        )

        if mode is DecisionMode.LLM_ONLY:
            if any(not check.passed for check in rails):
                return CycleResult(
                    mode=mode,
                    decision_input=decision_input,
                    proposal=proposal,
                    trust_report=None,
                    schedule_result=None,
                    execution_receipt=None,
                    outcome="proposal_rejected",
                    failure_reason="failed executor safety rails: "
                    + ", ".join(check.name for check in rails if not check.passed),
                )
            return self._execute(
                mode=mode,
                decision_input=decision_input,
                proposal=proposal,
                trust_report=None,
                schedule_result=None,
                fallback_triggered=False,
            )

        if mode is not DecisionMode.HYBRID:
            raise WorkflowError(f"unsupported decision mode: {mode.value}")

        trust_report = self._evaluate_hybrid_trust(
            decision_input=decision_input,
            proposal=proposal,
            rails=rails,
            documents=documents,
            graph_context=graph,
        )
        if not trust_report.passed:
            return self._run_milp(
                decision_input,
                fallback_triggered=True,
                failed_trust_report=trust_report,
            )
        return self._execute(
            mode=mode,
            decision_input=decision_input,
            proposal=proposal,
            trust_report=trust_report,
            schedule_result=None,
            fallback_triggered=False,
        )

    def _evaluate_hybrid_trust(
        self,
        *,
        decision_input: DecisionInput,
        proposal: ActionProposal,
        rails: list[Any],
        documents: list[str],
        graph_context: Mapping[str, Any],
    ) -> TrustReport:
        evaluator_error: str | None = None
        ragas_scores: RagasScores | None = None
        if self.trust_evaluator is None:
            evaluator_error = "RAGAS evaluator is not configured"
        else:
            try:
                with self.telemetry.span("scheduler.trust.evaluate"):
                    ragas_scores = self.trust_evaluator.evaluate(
                        decision_input=decision_input,
                        proposal=proposal,
                        retrieval_documents=documents,
                        canonical_policy=build_canonical_policy_record(decision_input, dict(graph_context)),
                    )
            except TrustEvaluationError as error:
                evaluator_error = str(error)
            except Exception as error:  # evaluator integrations fail closed
                evaluator_error = f"unexpected evaluator failure: {error}"

        components = TrustComponentScores(
            ragas_faithfulness=ragas_scores.faithfulness if ragas_scores else None,
            ragas_context_precision=ragas_scores.context_precision if ragas_scores else None,
            # Deterministic action rails are the safety authority.  If a NeMo
            # runtime is configured separately, it may add checks, but it cannot
            # override these checks or fabricate a score.
            nemo_rails=1.0 if rails and all(check.passed for check in rails) else 0.0,
            data_freshness=_freshness_score(decision_input),
            execution_feasibility=(
                1.0 if decision_input.cluster_available.get(proposal.target_region, False) else 0.0
            ),
        )
        return evaluate_trust(
            proposal,
            components=components,
            policy=self.trust_policy,
            rail_checks=rails,
            evaluator_error=evaluator_error,
        )

    def _run_milp(
        self,
        decision_input: DecisionInput,
        *,
        fallback_triggered: bool,
        failed_trust_report: TrustReport | None = None,
    ) -> CycleResult:
        request = build_scheduling_request(
            decision_input,
            default_region=self.default_region,
            latency_catalog=self.latency_catalog,
            slot_minutes=self.slot_minutes,
        )
        with self.telemetry.span("scheduler.milp.solve"):
            schedule_result = solve_schedule(request)
        if schedule_result.status != "selected":
            return CycleResult(
                mode=DecisionMode.HYBRID if fallback_triggered else DecisionMode.MILP_ONLY,
                decision_input=decision_input,
                proposal=None,
                trust_report=failed_trust_report,
                schedule_result=schedule_result,
                execution_receipt=None,
                outcome=schedule_result.status,
                failure_reason=schedule_result.reason,
            )
        proposal = proposal_from_schedule(schedule_result, decision_input)
        return self._execute(
            mode=DecisionMode.HYBRID if fallback_triggered else DecisionMode.MILP_ONLY,
            decision_input=decision_input,
            proposal=proposal,
            trust_report=failed_trust_report,
            schedule_result=schedule_result,
            fallback_triggered=fallback_triggered,
        )

    def _run_static_reference(self, decision_input: DecisionInput) -> CycleResult:
        proposal, reason = self._build_static_reference_proposal(decision_input)
        if proposal is None:
            return CycleResult(
                mode=DecisionMode.STATIC_REFERENCE,
                decision_input=decision_input,
                proposal=None,
                trust_report=None,
                schedule_result=None,
                execution_receipt=None,
                outcome="infeasible",
                failure_reason=reason,
            )
        return self._execute(
            mode=DecisionMode.STATIC_REFERENCE,
            decision_input=decision_input,
            proposal=proposal,
            trust_report=None,
            schedule_result=None,
            fallback_triggered=False,
        )

    def _build_static_reference_proposal(
        self,
        decision_input: DecisionInput,
    ) -> tuple[ActionProposal | None, str | None]:
        if self.default_region not in decision_input.candidate_regions:
            return None, "static reference region is not a candidate region"
        if not decision_input.cluster_available.get(self.default_region, False):
            return None, "static reference region is unavailable"
        expected_latency = self._expected_latency(decision_input.workload_profile, self.default_region)
        proposal = ActionProposal(
            action_type=ActionType.RUN_NOW,
            target_region=self.default_region,
            target_service=decision_input.dependency_context.target_service,
            scheduled_time=decision_input.earliest_start,
            workload_profile=decision_input.workload_profile,
            expected_p95_latency_ms=expected_latency,
            self_reported_confidence=0.0,
            evidence_ids=[],
            rationale="Static reference policy: run immediately in the configured default region.",
            model_name="static-reference",
            prompt_version="not-applicable",
            request_id=f"static-{decision_input.cycle_id}",
        )
        return proposal, None

    def _execute(
        self,
        *,
        mode: DecisionMode,
        decision_input: DecisionInput,
        proposal: ActionProposal,
        trust_report: TrustReport | None,
        schedule_result: ScheduleResult | None,
        fallback_triggered: bool,
    ) -> CycleResult:
        try:
            with self.telemetry.span("scheduler.executor.execute"):
                receipt = self.executor.execute(
                    action=proposal,
                    mode=mode,
                    run_id=decision_input.run_id,
                    cycle_id=decision_input.cycle_id,
                    carbon_snapshot_id=decision_input.carbon_snapshot.snapshot_id,
                    config_hash=decision_input.config_hash,
                    fallback_triggered=fallback_triggered,
                )
        except ExecutionError as error:
            return CycleResult(
                mode=mode,
                decision_input=decision_input,
                proposal=proposal,
                trust_report=trust_report,
                schedule_result=schedule_result,
                execution_receipt=None,
                outcome="execution_failed",
                failure_reason=str(error),
            )
        except Exception as error:
            return CycleResult(
                mode=mode,
                decision_input=decision_input,
                proposal=proposal,
                trust_report=trust_report,
                schedule_result=schedule_result,
                execution_receipt=None,
                outcome="execution_failed",
                failure_reason=f"unexpected executor failure: {error}",
            )

        receipt = _with_slo_status(receipt, decision_input)
        outcome = "dry_run" if (receipt.execution_failure_reason or "").startswith("dry-run:") else (
            "execution_failed" if receipt.execution_failure_reason else "executed"
        )
        return CycleResult(
            mode=mode,
            decision_input=decision_input,
            proposal=proposal,
            trust_report=trust_report,
            schedule_result=schedule_result,
            execution_receipt=receipt,
            outcome=outcome,
            failure_reason=receipt.execution_failure_reason,
        )

    def _failed_report(self, *, reason: str) -> TrustReport:
        return TrustReport(
            components=TrustComponentScores(),
            rail_checks=[],
            weighted_score=None,
            threshold=self.trust_policy.threshold,
            passed=False,
            failure_reason=reason,
        )

    def _expected_latency(self, workload_profile: str, region: str) -> float:
        try:
            value = self.latency_catalog[workload_profile][region]
        except KeyError as error:
            raise WorkflowError(
                f"latency catalog has no pilot measurement for profile {workload_profile!r} in region {region!r}"
            ) from error
        if value <= 0:
            raise WorkflowError("latency catalog values must be positive")
        return value


def load_latency_catalog(path: str | Path) -> dict[str, dict[str, float]]:
    """Load the pilot-derived `{profile: {region: p95_ms}}` YAML catalog."""

    source = Path(path)
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except OSError as error:
        raise WorkflowError(f"cannot read latency catalog: {source}") from error
    except yaml.YAMLError as error:
        raise WorkflowError(f"invalid latency catalog YAML: {source}") from error
    if not isinstance(raw, dict) or not raw:
        raise WorkflowError("latency catalog must be a nonempty mapping")
    result: dict[str, dict[str, float]] = {}
    for profile, values in raw.items():
        if not isinstance(profile, str) or not isinstance(values, dict) or not values:
            raise WorkflowError("latency catalog must map each profile to region values")
        region_values: dict[str, float] = {}
        for region, value in values.items():
            try:
                numeric = float(value)
            except (TypeError, ValueError) as error:
                raise WorkflowError(f"latency catalog value for {profile}/{region} is not numeric") from error
            if numeric <= 0:
                raise WorkflowError(f"latency catalog value for {profile}/{region} must be positive")
            region_values[str(region)] = numeric
        result[profile] = region_values
    return result


def build_scheduling_request(
    decision_input: DecisionInput,
    *,
    default_region: str,
    latency_catalog: Mapping[str, Mapping[str, float]],
    slot_minutes: int | None = None,
) -> SchedulingRequest:
    """Create current/forecast candidates directly from a frozen carbon input."""

    try:
        latency_by_region = latency_catalog[decision_input.workload_profile]
    except KeyError as error:
        raise WorkflowError(
            f"latency catalog has no profile {decision_input.workload_profile!r}"
        ) from error
    if slot_minutes is not None and slot_minutes <= 0:
        raise ValueError("slot_minutes must be positive when supplied")
    candidates: list[ScheduleCandidate] = []
    for region in decision_input.candidate_regions:
        if region not in latency_by_region:
            raise WorkflowError(
                f"latency catalog has no pilot value for profile {decision_input.workload_profile!r} "
                f"in region {region!r}"
            )
        region_data = decision_input.carbon_snapshot.regions[region]
        common = {
            "target_region": region,
            "expected_p95_latency_ms": float(latency_by_region[region]),
            "region_available": decision_input.cluster_available.get(region, False),
            "reset_ready": True,
        }
        candidates.append(
            ScheduleCandidate(
                **common,
                scheduled_time=decision_input.earliest_start,
                carbon_intensity_gco2eq_per_kwh=region_data.current.carbon_intensity_gco2eq_per_kwh,
            )
        )
        for point in region_data.forecast:
            if not decision_input.earliest_start <= point.timestamp <= decision_input.deadline:
                continue
            if slot_minutes is not None:
                elapsed_seconds = (
                    point.timestamp - decision_input.earliest_start
                ).total_seconds()
                interval_seconds = slot_minutes * 60
                # Forecast timestamps normally land exactly on the requested
                # granularity.  Permit a one-second boundary tolerance for
                # provider formatting, but do not invent/interpolate values.
                remainder = elapsed_seconds % interval_seconds
                if min(remainder, interval_seconds - remainder) > 1.0:
                    continue
            candidates.append(
                ScheduleCandidate(
                    **common,
                    scheduled_time=point.timestamp,
                    carbon_intensity_gco2eq_per_kwh=point.carbon_intensity_gco2eq_per_kwh,
                )
            )
    return SchedulingRequest(
        workload_profile=decision_input.workload_profile,
        default_region=default_region,
        earliest_start=decision_input.earliest_start,
        deadline=decision_input.deadline,
        p95_slo_limit_ms=decision_input.p95_slo_limit_ms,
        candidates=candidates,
    )


def proposal_from_schedule(
    schedule: ScheduleResult,
    decision_input: DecisionInput,
) -> ActionProposal:
    """Convert a selected solver result into the shared execution contract."""

    if schedule.status != "selected":
        raise WorkflowError("only selected schedules can become executable proposals")
    assert schedule.action_type is not None
    assert schedule.target_region is not None
    assert schedule.scheduled_time is not None
    assert schedule.expected_p95_latency_ms is not None
    return ActionProposal(
        action_type=schedule.action_type,
        target_region=schedule.target_region,
        target_service=decision_input.dependency_context.target_service,
        scheduled_time=schedule.scheduled_time,
        workload_profile=decision_input.workload_profile,
        expected_p95_latency_ms=schedule.expected_p95_latency_ms,
        self_reported_confidence=0.0,
        evidence_ids=[],
        rationale="Deterministic scheduling result from the frozen carbon and latency inputs.",
        model_name="pulp-milp" if schedule.solver_backend == "pulp" else "deterministic-enumeration",
        prompt_version="not-applicable",
        request_id=f"milp-{decision_input.cycle_id}",
    )


def _freshness_score(decision_input: DecisionInput) -> float:
    """Use a declared binary freshness rule rather than an invented decay curve."""

    for region in decision_input.carbon_snapshot.regions.values():
        # A replayed snapshot may carry the original cache fallback reason.
        # It is reproducible, but not fresh enough to grant the live-data
        # component full credit.
        if region.source is CarbonDataSource.CACHE:
            return 0.0
        if region.source is CarbonDataSource.REPLAY and region.fallback_reason:
            return 0.0
        if region.source not in {CarbonDataSource.LIVE, CarbonDataSource.REPLAY}:
            return 0.0
    return 1.0


def _agent_failure_reason(error: AgentError) -> str:
    """Keep tunnel/connectivity failures distinct from invalid model output."""

    if isinstance(error, AgentUnavailableError):
        return f"llm_unavailable: {error}"
    if isinstance(error, AgentResponseError):
        return f"llm_response_invalid: {error}"
    return f"llm_agent_error: {error}"


def _with_slo_status(receipt: ExecutionReceipt, decision_input: DecisionInput) -> ExecutionReceipt:
    if receipt.execution_failure_reason:
        # A failed/dry-run executor produced no workload observation.  Keep
        # SLO status unknown instead of turning infrastructure failure into a
        # fabricated violation.
        return receipt.model_copy(update={"slo_passed": None})
    if receipt.actual_p95_latency_ms is None or receipt.actual_error_rate is None:
        return receipt
    return receipt.model_copy(
        update={
            "slo_passed": (
                receipt.actual_p95_latency_ms <= decision_input.p95_slo_limit_ms
                and receipt.actual_error_rate <= decision_input.error_rate_limit
            )
        }
    )


def main() -> int:
    """Entry-point hint for users who invoke `carbon-scheduler` directly."""

    from scripts.run_experiment import main as run_experiment_main

    return run_experiment_main()


if __name__ == "__main__":  # pragma: no cover - CLI delegation
    raise SystemExit(main())
