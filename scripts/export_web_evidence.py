"""Export dated, recorded evidence for the website (web/data/evidence.json).

The experiment results live under paper/ (unpublished, gitignored). This script
copies only aggregate numbers and one recorded receipt into a small committed
JSON file, so the dashboard never has to invent or hard-code a result.

Sources:
* live AKS session of 2026-10-12, gate v2.1:
  paper/experiments/results/live-aks-20261012/live_runs.csv and runs/<id>/
* offline evaluation: paper/experiments/results/analysis/key_numbers.json

Usage: uv run python scripts/export_web_evidence.py
"""

from __future__ import annotations

import csv
import json
import re
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SESSION = REPO / "paper/experiments/results/live-aks-20261012"
KEYS = REPO / "paper/experiments/results/analysis/key_numbers.json"
OUT = REPO / "web/data/evidence.json"
EXAMPLE_RUN = "lvd-clean08-hybrid-v21"  # the one live run in which v2.1 admitted the LLM proposal

LABEL = {"static": "Static (eastus2, carbon-agnostic)", "milp": "MILP only (exact program)",
         "llm": "LLM only (no gate)", "hybrid-v21": "Hybrid, gate v2.1"}


def _num(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def live_session() -> dict:
    rows = list(csv.DictReader((SESSION / "live_runs.csv").open()))
    executed = [r for r in rows if r["outcome"] == "executed"]
    p95: dict[str, list[float]] = {}
    for r in executed:
        p95.setdefault(r["executed_region"], []).append(float(r["measured_p95_ms"]))
    policies = []
    for kind in ("clean", "fault"):
        for policy in ("static", "milp", "llm", "hybrid-v21"):
            group = [r for r in rows if r["kind"] == kind and r["policy"] == policy]
            if not group:
                continue
            ran = [r["executed_region"] or "held" for r in group]
            true_ci = [_num(r["carbon_true_gco2_kwh"]) for r in group if _num(r["carbon_true_gco2_kwh"]) is not None]
            optimum = [min(float(r["true_eastus2"]), float(r["true_westus2"])) for r in group
                       if r["true_eastus2"] and r["true_westus2"]]
            p = [_num(r["measured_p95_ms"]) for r in group if _num(r["measured_p95_ms"]) is not None]
            slo = [r for r in group if r["outcome"] == "executed"]
            policies.append({
                "rounds": kind, "policy": policy, "label": LABEL[policy], "n": len(group),
                "ran_eastus2": ran.count("eastus2"), "ran_westus2": ran.count("westus2"), "held": ran.count("held"),
                "llm_proposed_eastus2": sum(r["proposed_region"] == "eastus2" for r in group) if policy in ("llm", "hybrid-v21") else None,
                "llm_proposed_westus2": sum(r["proposed_region"] == "westus2" for r in group) if policy in ("llm", "hybrid-v21") else None,
                "admitted_llm": sum(r["decision_source"] == "llm" for r in group) if policy == "hybrid-v21" else None,
                "mean_true_ci": round(statistics.mean(true_ci)) if true_ci else None,
                "mean_optimum_ci": round(statistics.mean(optimum)) if optimum else None,
                "median_p95_ms": round(statistics.median(p), 1) if p else None,
                "slo_passed": sum(r["slo_passed_receipt"] == "True" for r in slo),
                "sessions": len(slo),
                "errors": int(sum(float(r["error_rate_from_log"] or 0) * float(r["requests"] or 0) for r in slo)),
            })
    faults = [r for r in rows if r["kind"] == "fault"]
    seen = sorted({float(r["carbon_seen_gco2_kwh"]) for r in faults if r["carbon_seen_gco2_kwh"]})
    runs = [{
        "round": r["round"], "kind": r["kind"], "policy": r["policy"], "outcome": r["outcome"],
        "decision_source": r["decision_source"], "proposed_region": r["proposed_region"] or None,
        "executed_region": r["executed_region"] or None, "true_ci": _num(r["carbon_true_gco2_kwh"]),
        "p95_ms": _num(r["measured_p95_ms"]), "slo_passed": r["slo_passed_receipt"] == "True" if r["outcome"] == "executed" else None,
        "started_at": r["started_at"], "failure_reason": (r["failure_reason"] or "")[:160] or None,
    } for r in rows]
    return {
        "date": "2026-10-12", "gate_profile": "v2.1", "cluster_pair": ["aks-primary-eastus2", "aks-secondary-westus2"],
        "workload": "DeathStarBench Social Network, wrk2 compose-post.lua, 10 req/s, 2 threads, 2 connections, 60 s, after a fixture reset",
        "carbon_source": "Electricity Maps zone-keyed capture per round (US-MIDA-PJM, US-NW-BPAT); fault rounds use the Azure-region lookup",
        "first_run_utc": min(r["started_at"] for r in rows), "last_run_utc": max(r["ended_at"] or r["started_at"] for r in rows),
        "runs": len(rows), "clean_rounds": len({r["round"] for r in rows if r["kind"] == "clean"}),
        "fault_rounds": len({r["round"] for r in faults}), "executed_sessions": len(executed),
        "dispatched_in_window": sum(_num(r["start_after_deadline_s"]) is not None and float(r["start_after_deadline_s"]) <= 0 for r in executed),
        "slo_verdict_recorded": sum(r["slo_passed_receipt"] in ("True", "False") for r in executed),
        "slo_passed": sum(r["slo_passed_receipt"] == "True" for r in executed),
        "data_integrity_holds": sum(r["outcome"] == "data_integrity_hold" for r in rows),
        "p95_ms_by_region": {k: {"min": round(min(v), 1), "median": round(statistics.median(v), 1), "max": round(max(v), 1), "n": len(v)}
                             for k, v in sorted(p95.items())},
        "fault_round_seen_ci": seen, "fault_round_returned_zone": "IN-WE",
        "policies": policies, "run_rows": runs,
    }


def example_receipt() -> dict:
    run = SESSION / "runs" / EXAMPLE_RUN
    cycle = json.loads((run / "cycles.jsonl").read_text().splitlines()[0])
    receipt = cycle["execution_receipt"]
    proposal = cycle.get("agent_proposal") or {}
    trust = cycle.get("trust_report") or {}
    log = next((run / "benchmark-logs").glob("*-benchmark.log")).read_text(errors="replace")
    # Keep the summary; drop wrk2's long per-percentile spectrum table.
    keep = [line.rstrip() for line in log.splitlines()
            if line.strip() and not re.match(r"^\s+[0-9.]+\s+[0-9.]+\s+[0-9]+\s+[0-9.inf]+$", line)
            and "Detailed Percentile spectrum" not in line and "Value   Percentile" not in line]
    comps = trust.get("components") or {}
    return {
        "run_id": EXAMPLE_RUN, "gate_profile": receipt.get("gate_profile"), "decision_source": cycle.get("decision_source"),
        "cluster": receipt.get("cluster_name"), "region": (receipt.get("final_action") or {}).get("target_region"),
        "window": [cycle["decision_input"]["earliest_start"], cycle["decision_input"]["deadline"]],
        "dispatched_at": receipt.get("dispatched_at"), "completed_at": receipt.get("completed_at"),
        "proposal": {k: proposal.get(k) for k in ("target_region", "action_type", "scheduled_time", "expected_p95_latency_ms")},
        "checks_passed": [c["name"] for c in trust.get("rail_checks", []) if c.get("passed")],
        "checks_failed": [c["name"] for c in trust.get("rail_checks", []) if not c.get("passed")],
        "carbon_regret": trust.get("carbon_regret"),
        "ragas_faithfulness": round(comps.get("ragas_faithfulness", 0), 3) if comps else None,
        "ragas_context_precision": round(comps.get("ragas_context_precision", 0), 3) if comps else None,
        "score": round(trust.get("weighted_score"), 3) if trust.get("weighted_score") is not None else None,
        "p95_ms": receipt.get("actual_p95_latency_ms"), "error_rate": receipt.get("actual_error_rate"),
        "slo_passed": receipt.get("slo_passed"), "job_name": receipt.get("job_name"),
        "log_excerpt": keep,
    }


def offline() -> list[dict]:
    keys = json.loads(KEYS.read_text())

    def v(key: str):
        return keys[key]["value"]

    return [
        {"claim": "Unsafe random/adversarial proposals admitted by gate v2.1", "value": "0 of 4,405",
         "detail": "Wilson 95% upper bound 0.087%; 2,400 proposals per cell, fixed evaluator", "source": "r2random.*"},
        {"claim": "Same corpus, westus2 latency above the SLO, gate v2 (no model-feasibility check)", "value": "242 of 2,110",
         "detail": "every one in a region whose catalog P95 exceeds the SLO", "source": "r2random.west_slo.v2.unsafe_admitted"},
        {"claim": "Prompt injection followed by the ungated LLM / admitted by v2.1", "value": "72 of 72 / 0 of 72",
         "detail": "Llama-3 8B, 24 decisions x 3 seeds; v2.1 rejected via the carbon-regret check", "source": "e1.* / F1_safety.poisoned_context"},
        {"claim": "Injected SLO-infeasible westus2 proposals admitted, v2 vs v2.1", "value": "19 of 48 vs 0 of 48",
         "detail": "westus2 catalog P95 600 ms > 500 ms SLO", "source": "e4.west_slo.*"},
        {"claim": "Real region-name lookups flagged by the identity checks", "value": "15 of 15",
         "detail": "all returned an Indian grid zone for both US regions; 0 of 26 zone-keyed captures flagged", "source": "r2prov.* / live_captures_audit.csv"},
        {"claim": "False positives of the identical-series check on clean data", "value": "0 of 61,304",
         "detail": "PJM-BPAT decisions, 2019-2024", "source": "r2prov.*"},
        {"claim": "Faults the checks cannot see", "value": "units x1000, copied forecast",
         "detail": "each reached the action in 24 of 24 decisions", "source": "r2heldout.*"},
        {"claim": "Safe random proposals rejected (false rejects)", "value": "about 30%",
         "detail": "all by the declared-latency check", "source": "r2random.*.false_reject_rate"},
        {"claim": "Clean LLM proposals admitted offline", "value": "0 of 72",
         "detail": "the LLM proposed the dirtier region every time; the regret check rejected it", "source": "e5.hybrid_v2.1.stats"},
        {"claim": "Evidence (RAGAS) score separating good from bad proposals", "value": f"AUROC {v('e3rules.auroc_score_truth_regret_le_5pct')['auroc']}",
         "detail": "close to chance; the hard checks, not the score, keep dirty proposals out", "source": "e3rules.*"},
        {"claim": "Exact program vs static eastus2 / always westus2", "value": "61.2% / 10.3% lower intensity",
         "detail": "24 stratified decisions of 2023, OGE v0.8.0; the gain over westus2 is timing", "source": "e5.*"},
        {"claim": "Decision time, gate v2.1", "value": "median 9.7 s",
         "detail": "36.7 s when the RAGAS judge runs; program solve 3.6 ms", "source": "e8.*"},
    ]


def main() -> int:
    if not SESSION.exists() or not KEYS.exists():
        print(f"evidence sources missing ({SESSION} / {KEYS}); web/data/evidence.json left unchanged")
        return 1
    evidence = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "note": "Recorded results only. Nothing here is live; the live carbon feed and cluster checks come from /api/live-carbon and /api/cluster-status.",
        "live_session": live_session(),
        "example_receipt": example_receipt(),
        "offline": offline(),
        "withdrawn": {
            "run_id": "real-milp-pilot-v2", "date": "2026-09-07",
            "reason": "The load generator could not load compose-post.lua and measured plain GET /, and the carbon lookup returned an Indian grid zone (IN-SO) for both regions. Its latency, error and carbon figures are not valid.",
        },
    }
    OUT.write_text(json.dumps(evidence, indent=1) + "\n")
    print(f"wrote {OUT.relative_to(REPO)} ({OUT.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
