"""Decision flow for LLM-only, MILP-only, and trust-gated hybrid modes."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, TypedDict

import yaml

from .agent import (
    AgentContextOverflowError,
    AgentError,
    AgentResponseError,
    AgentUnavailableError,
    ProposalAgent,
)
from .evaluation import RagasScores, TrustEvaluationError, TrustEvaluator, build_canonical_policy_record
from .executor import BenchmarkExecutor, DryRunExecutor, ExecutionError
from .milp import solve_schedule
from .schemas import (
    ActionProposal,
    ActionType,
    CarbonDataSource,
    CarbonSnapshot,
    DecisionInput,
    DecisionMode,
    ExecutionReceipt,
    GateProfile,
    RailCheck,
    ScheduleCandidate,
    ScheduleResult,
    SchedulingRequest,
    TrustComponentScores,
    TrustPolicy,
    TrustReport,
)
from .trust import (
    GatePolicy,
    check_carbon_regret,
    check_data_age,
    check_evidence,
    check_model_feasibility,
    check_zone_provenance,
    evaluate_trust,
    evaluate_trust_v2,
    observation_ages,
    snapshot_data_age_seconds,
    validate_proposal,
)
from .telemetry import NoopTelemetry, Telemetry


class WorkflowError(RuntimeError):
    """A workflow prerequisite was missing or inconsistent."""


# v2 data-integrity alert levels (CycleResult.alert_level / ExecutionReceipt.alert_level)
ALERT_WARNING = "warning"  # last-known-good fallback after a data-rail failure
ALERT_CRITICAL = "critical"  # data-integrity hold: no new session


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
    # The LLM's own proposal, kept even when the gate rejected it (the
    # ``proposal`` field then holds the MILP fallback action).
    agent_proposal: ActionProposal | None = None
    gate_profile: GateProfile | None = None
    # llm | milp | milp_last_known_good | data_integrity_hold | static_reference
    decision_source: str | None = None
    alert: bool = False
    # v2 data-integrity handling: "warning" = last-known-good fallback (decided
    # on older clean data), "critical" = hold (no new session).  None otherwise.
    alert_level: str | None = None
    # v2 hybrid: {"passed": bool, "failed_rails": [...], "reasons": [...]}
    data_integrity: dict[str, Any] | None = None
    # v2 last-known-good fallback: {"snapshot_id": ..., "age_seconds": ...}
    last_known_good: dict[str, Any] | None = None

    def to_record(self) -> dict[str, Any]:
        """Convert to a JSON-safe event record without dropping failed states."""

        return {
            "record_type": "scheduler_cycle",
            "mode": self.mode.value,
            "gate_profile": self.gate_profile.value if self.gate_profile else None,
            "outcome": self.outcome,
            "failure_reason": self.failure_reason,
            "agent_proposal": self.agent_proposal.model_dump(mode="json") if self.agent_proposal else None,
            "decision_source": self.decision_source,
            "alert": self.alert,
            "alert_level": self.alert_level,
            "data_integrity": self.data_integrity,
            "last_known_good": self.last_known_good,
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
        gate: GatePolicy | None = None,
        clock: Callable[[], datetime] | None = None,
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
        # Default is the corrected gate; GatePolicy(profile=AS_BUILT) restores
        # the original behaviour exactly.
        self.gate = gate or GatePolicy()
        self._agent_proposal: ActionProposal | None = None
        self._reference: tuple[tuple[str, str], SchedulingRequest, ScheduleResult] | None = None
        # Per-run v2 state: the newest snapshot that passed the data rails and
        # the region of the last placed session (kept by a data-integrity hold).
        self._last_known_good: CarbonSnapshot | None = None
        self._last_placement: str | None = None
        # Scheduler clock.  A real executor gets the wall clock by default and
        # every receipt records the dispatch time.  Under the v2 family, data
        # age is then also measured against it and actions past the deadline
        # are refused (as_built keeps its original behaviour: no refusal).
        # Dry-run without an explicit clock keeps the snapshot's (trace) time.
        real_executor = not isinstance(executor, DryRunExecutor)
        self.dispatch_checks_deadline = real_executor and self.gate.profile.is_v2_family
        self.clock = clock or (_wall_clock if real_executor else None)
        self._cycle_now: datetime | None = None

    def restore_state(
        self,
        *,
        last_known_good: CarbonSnapshot | None,
        last_placement: str | None,
    ) -> None:
        """Re-seed per-run state when a run is resumed from saved records."""

        self._last_known_good = last_known_good
        self._last_placement = last_placement

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
            self._agent_proposal = None
            self._reference = None
            self._cycle_now = self.clock() if self.clock is not None else None
            data_integrity: dict[str, Any] | None = None
            if mode is DecisionMode.HYBRID and self.gate.profile.is_v2_family:
                data_checks = self._data_rails(decision_input)
                failed = [check for check in data_checks if not check.passed]
                data_integrity = {
                    "passed": not failed,
                    "failed_rails": [check.name for check in failed],
                    "reasons": [check.reason for check in failed],
                }
                if failed:
                    # Never run the MILP (or the LLM) on inputs that failed a
                    # data rail: last-known-good data, else hold.
                    return self._stamp(self._data_integrity_response(decision_input, failed, data_integrity))
                self._last_known_good = decision_input.carbon_snapshot
            if self.orchestration_backend == "langgraph":
                result = self._run_cycle_langgraph(
                    mode=mode,
                    decision_input=decision_input,
                    graph_context=graph_context,
                    retrieval_documents=retrieval_documents,
                )
            else:
                result = self._run_cycle(
                    mode=mode,
                    decision_input=decision_input,
                    graph_context=graph_context,
                    retrieval_documents=retrieval_documents,
                )
            if data_integrity is not None:
                result = replace(result, data_integrity=data_integrity)
            return self._stamp(result)

    def _stamp(self, result: CycleResult) -> CycleResult:
        """Record profile, LLM proposal and decision source on every cycle."""

        profile = self.gate.profile
        receipt = result.execution_receipt
        source = result.decision_source
        if source is None and receipt is not None:
            if result.mode is DecisionMode.STATIC_REFERENCE:
                source = "static_reference"
            elif result.mode is DecisionMode.LLM_ONLY:
                source = "llm"
            else:
                source = "milp" if receipt.fallback_triggered or result.mode is DecisionMode.MILP_ONLY else "llm"
        updates: dict[str, Any] = {
            "gate_profile": profile,
            "agent_proposal": self._agent_proposal,
            "decision_source": source,
        }
        if result.trust_report is not None:
            updates["trust_report"] = result.trust_report.model_copy(update={"gate_profile": profile})
        if receipt is not None:
            updates["execution_receipt"] = receipt.model_copy(
                update={
                    "gate_profile": profile,
                    "decision_source": source,
                    "alert": result.alert,
                    "alert_level": result.alert_level,
                }
            )
            if result.outcome in {"dry_run", "executed"}:
                self._last_placement = receipt.final_action.target_region
        return replace(result, **updates)

    # ---------------------------------------------------------------- v2 data integrity
    def _data_rails(self, decision_input: DecisionInput) -> list[RailCheck]:
        """Rails about the decision INPUTS (not the proposal): age and zone provenance."""

        age_check, _ = check_data_age(
            decision_input.carbon_snapshot, max_age_seconds=self.gate.max_data_age_seconds, now=self._cycle_now
        )
        provenance = check_zone_provenance(
            decision_input.carbon_snapshot,
            regions=decision_input.candidate_regions,
            expected_provider_regions=self.gate.expected_provider_regions,
            earliest_start=decision_input.earliest_start,
            deadline=decision_input.deadline,
            expected_zones=self.gate.expected_zones,
        )
        return [age_check, provenance]

    def _data_integrity_response(
        self,
        decision_input: DecisionInput,
        failed: list[RailCheck],
        data_integrity: dict[str, Any],
    ) -> CycleResult:
        """v2: MILP on the last-known-good snapshot if recent enough, else hold.

        The failing inputs are used for neither the LLM nor the MILP.
        """

        reason = "data integrity: " + "; ".join(f"{check.name}: {check.reason}" for check in failed)
        report = TrustReport(
            components=TrustComponentScores(),
            rail_checks=failed,
            weighted_score=None,
            threshold=self.trust_policy.threshold,
            passed=False,
            failure_reason=reason,
            gate_profile=GateProfile.V2,
            data_age_seconds=snapshot_data_age_seconds(
                decision_input.carbon_snapshot, decision_input.carbon_snapshot.decision_time
            ),
            clock_data_age_seconds=(
                snapshot_data_age_seconds(decision_input.carbon_snapshot, self._cycle_now)
                if self._cycle_now is not None
                else None
            ),
        )
        lkg = self._last_known_good
        if lkg is not None:
            # v2.1: last-known-good age against the scheduler clock when there
            # is one (the failing snapshot's own decision_time is untrusted)
            age = snapshot_data_age_seconds(lkg, self._cycle_now or decision_input.carbon_snapshot.decision_time)
            if age <= self.gate.last_known_good_max_age_seconds:
                lkg_input = decision_input.model_copy(update={"carbon_snapshot": lkg})
                try:
                    request = build_scheduling_request(
                        lkg_input,
                        default_region=self.default_region,
                        latency_catalog=self.latency_catalog,
                        slot_minutes=self.slot_minutes,
                    )
                    with self.telemetry.span("scheduler.milp.solve"):
                        schedule_result = solve_schedule(request)
                except Exception:  # unusable last-known-good inputs -> hold
                    schedule_result = None
                if schedule_result is not None and schedule_result.status == "selected":
                    result = self._execute(
                        mode=DecisionMode.HYBRID,
                        decision_input=decision_input,
                        proposal=proposal_from_schedule(schedule_result, lkg_input),
                        trust_report=report,
                        schedule_result=schedule_result,
                        fallback_triggered=True,
                    )
                    # Decided on older clean data: operator attention is
                    # warranted, but less urgently than a hold.
                    return replace(
                        result,
                        decision_source="milp_last_known_good",
                        alert=True,
                        alert_level=ALERT_WARNING,
                        data_integrity=data_integrity,
                        last_known_good={"snapshot_id": lkg.snapshot_id, "age_seconds": age},
                    )
                reason += "; last-known-good MILP not selectable"
            else:
                reason += f"; last-known-good snapshot too old ({age:.0f}s)"
        else:
            reason += "; no last-known-good snapshot"
        return self._hold(decision_input, report.model_copy(update={"failure_reason": reason}), data_integrity)

    def _hold(
        self,
        decision_input: DecisionInput,
        report: TrustReport,
        data_integrity: dict[str, Any],
    ) -> CycleResult:
        """No new session: keep the previous placement (first cycle: static region).

        The executor is deliberately NOT called, so a hold is a no-op for the
        Kubernetes and dry-run executors alike; the receipt only records it.
        """

        region = self._last_placement or self.default_region
        hold_action = ActionProposal(
            action_type=ActionType.RUN_NOW,
            target_region=region,
            target_service=decision_input.dependency_context.target_service,
            scheduled_time=None,
            workload_profile=decision_input.workload_profile,
            expected_p95_latency_ms=self._expected_latency(decision_input.workload_profile, region),
            self_reported_confidence=0.0,
            evidence_ids=[],
            rationale="Data-integrity hold: no session started; placement stays in the held region.",
            model_name="data-integrity-hold",
            prompt_version="not-applicable",
            request_id=f"hold-{decision_input.cycle_id}",
        )
        receipt = ExecutionReceipt(
            run_id=decision_input.run_id,
            cycle_id=decision_input.cycle_id,
            mode=DecisionMode.HYBRID,
            final_action=hold_action,
            fallback_triggered=True,
            execution_failure_reason="hold: " + (report.failure_reason or "data integrity"),
            decision_source="data_integrity_hold",
            alert=True,
            alert_level=ALERT_CRITICAL,
        )
        return CycleResult(
            mode=DecisionMode.HYBRID,
            decision_input=decision_input,
            proposal=None,
            trust_report=report,
            schedule_result=None,
            execution_receipt=receipt,
            outcome="data_integrity_hold",
            failure_reason=report.failure_reason,
            decision_source="data_integrity_hold",
            alert=True,
            alert_level=ALERT_CRITICAL,
            data_integrity=data_integrity,
        )

    def _reference_schedule(self, decision_input: DecisionInput) -> tuple[SchedulingRequest, ScheduleResult]:
        """Build and solve the MILP once per cycle (v2 regret rail + fallback share it)."""

        key = (decision_input.run_id, decision_input.cycle_id)
        if self._reference is not None and self._reference[0] == key:
            return self._reference[1], self._reference[2]
        request = build_scheduling_request(
            decision_input,
            default_region=self.default_region,
            latency_catalog=self.latency_catalog,
            slot_minutes=self.slot_minutes,
        )
        with self.telemetry.span("scheduler.milp.solve"):
            schedule_result = solve_schedule(request)
        self._reference = (key, request, schedule_result)
        return request, schedule_result

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

        self._agent_proposal = proposal
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
            _, schedule_result = self._reference_schedule(decision_input)
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

        self._agent_proposal = proposal
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
        if self.gate.profile.is_v2_family:
            return self._evaluate_hybrid_trust_v2(
                decision_input=decision_input,
                proposal=proposal,
                rails=rails,
                documents=documents,
                graph_context=graph_context,
            )
        # ---- as_built gate (unchanged) ----
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

    def _evaluate_hybrid_trust_v2(
        self,
        *,
        decision_input: DecisionInput,
        proposal: ActionProposal,
        rails: list[Any],
        documents: list[str],
        graph_context: Mapping[str, Any],
    ) -> TrustReport:
        """Corrected gate: all hard rails first, RAGAS only for rail-clean proposals.

        Hard rails = the seven deterministic proposal rails + data age + zone
        provenance + cited-evidence validity + carbon regret vs the MILP
        optimum.  A failed hard rail rejects outright (no score can override
        it) and skips the RAGAS calls.  Otherwise the RAGAS-only weighted score
        must reach ``trust.threshold`` (``evaluate_trust_v2``).
        """

        snapshot = decision_input.carbon_snapshot
        age_check, data_age = check_data_age(
            snapshot, max_age_seconds=self.gate.max_data_age_seconds, now=self._cycle_now
        )
        provenance = check_zone_provenance(
            snapshot,
            regions=decision_input.candidate_regions,
            expected_provider_regions=self.gate.expected_provider_regions,
            earliest_start=decision_input.earliest_start,
            deadline=decision_input.deadline,
            expected_zones=self.gate.expected_zones,
        )
        evidence = check_evidence(proposal, allowed_evidence_ids=decision_input.retrieval_context_ids)
        try:
            request, reference = self._reference_schedule(decision_input)
        except Exception:  # missing latency/candidate data -> regret rail fails closed
            request, reference = None, None
        regret_check, proposal_ci, reference_ci, regret = check_carbon_regret(
            proposal,
            request=request,
            reference=reference,
            tolerance=self.gate.carbon_regret_tolerance,
        )
        checks = [*rails, age_check, provenance, evidence, regret_check]
        if self.gate.feasibility_rail_enabled:
            checks.append(check_model_feasibility(proposal, request=request))
        extra = {
            "data_age_seconds": data_age,
            "clock_data_age_seconds": (
                max(observation_ages(snapshot, self._cycle_now).values()) if self._cycle_now is not None else None
            ),
            "proposal_carbon_intensity_gco2eq_per_kwh": proposal_ci,
            "reference_carbon_intensity_gco2eq_per_kwh": reference_ci,
            "carbon_regret": regret,
        }
        components = TrustComponentScores(
            nemo_rails=1.0 if rails and all(check.passed for check in rails) else 0.0,
            data_freshness=1.0 if age_check.passed else 0.0,
            execution_feasibility=(
                1.0 if decision_input.cluster_available.get(proposal.target_region, False) else 0.0
            ),
        )
        if any(not check.passed for check in checks):
            report = evaluate_trust_v2(proposal, components=components, policy=self.trust_policy, rail_checks=checks)
            return report.model_copy(update=extra)

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
        if ragas_scores is not None:
            components = components.model_copy(
                update={
                    "ragas_faithfulness": ragas_scores.faithfulness,
                    "ragas_context_precision": ragas_scores.context_precision,
                }
            )
        report = evaluate_trust_v2(
            proposal,
            components=components,
            policy=self.trust_policy,
            rail_checks=checks,
            evaluator_error=evaluator_error,
        )
        return report.model_copy(update=extra)

    def _run_milp(
        self,
        decision_input: DecisionInput,
        *,
        fallback_triggered: bool,
        failed_trust_report: TrustReport | None = None,
    ) -> CycleResult:
        _, schedule_result = self._reference_schedule(decision_input)
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
        dispatched_at = self.clock() if self.clock is not None else None
        if self.dispatch_checks_deadline and dispatched_at is not None and dispatched_at > decision_input.deadline:
            # Real execution only: the rails checked the window against frozen
            # time; refuse an action whose window closed before dispatch.
            reason = (
                f"deadline passed: dispatch at {dispatched_at.isoformat()} is after the deadline "
                f"{decision_input.deadline.isoformat()}; no session started"
            )
            return CycleResult(
                mode=mode,
                decision_input=decision_input,
                proposal=proposal,
                trust_report=trust_report,
                schedule_result=schedule_result,
                execution_receipt=ExecutionReceipt(
                    run_id=decision_input.run_id,
                    cycle_id=decision_input.cycle_id,
                    mode=mode,
                    final_action=proposal,
                    fallback_triggered=fallback_triggered,
                    execution_failure_reason=reason,
                    alert=True,
                    alert_level=ALERT_CRITICAL,
                    dispatched_at=dispatched_at,
                ),
                outcome="deadline_passed",
                failure_reason=reason,
                alert=True,
                alert_level=ALERT_CRITICAL,
            )
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
        if dispatched_at is not None:
            receipt = receipt.model_copy(update={"dispatched_at": dispatched_at})
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


def trim_snapshot_to_window(
    snapshot: CarbonSnapshot,
    *,
    earliest_start: datetime,
    deadline: datetime,
    slot_minutes: int | None,
) -> CarbonSnapshot:
    """Keep only forecast points the MILP can use (v2 compact context).

    Same filter as ``build_scheduling_request`` (window bounds and slot grid
    with a one-second tolerance), so MILP decisions are unchanged while the
    LLM prompt and the retrieved snapshot document shrink to the decision
    window.  ``current`` is always kept.
    """

    interval = slot_minutes * 60 if slot_minutes else None
    regions = {}
    for name, data in snapshot.regions.items():
        kept = []
        for point in data.forecast:
            if not earliest_start <= point.timestamp <= deadline:
                continue
            if interval is not None:
                remainder = (point.timestamp - earliest_start).total_seconds() % interval
                if min(remainder, interval - remainder) > 1.0:
                    continue
            kept.append(point)
        regions[name] = data.model_copy(update={"forecast": kept})
    return snapshot.model_copy(update={"regions": regions})


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


def _wall_clock() -> datetime:
    return datetime.now(UTC)


def _agent_failure_reason(error: AgentError) -> str:
    """Keep tunnel/connectivity failures distinct from invalid model output."""

    if isinstance(error, AgentContextOverflowError):
        return f"context_overflow: {error}"
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
