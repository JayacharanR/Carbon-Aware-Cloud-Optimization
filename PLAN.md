# Detailed Implementation Plan: Trust-Gated Carbon-Aware Scheduling Prototype

> Agent handoff and plug-and-play completion status: see
> [AGENT_IMPLEMENTATION_PLAN.md](AGENT_IMPLEMENTATION_PLAN.md). The companion
> `.env.example`, `scripts/bootstrap.py`, and
> `scripts/install-prerequisites.ps1` now provide the operator-to-runtime
> configuration path. See [PLUG_AND_PLAY_SETUP.md](PLUG_AND_PLAY_SETUP.md) for
> where each external value comes from; real Azure/API/DeathStarBench values remain external
> inputs and are never invented.

## 1. Project definition and fixed scope

### Goal

Build a research prototype that evaluates whether a trust-gated LLM scheduler, with MILP fallback, produces better carbon-intensity-aware placement decisions and SLO outcomes than:

1. LLM-only scheduling
2. MILP-only scheduling
3. Trust-gated hybrid scheduling

The application workload is DeathStarBench Social Network running on Azure Kubernetes Service in two Azure regions.

### What “regional scheduling” means

This project does not migrate live pods, databases, users, or sessions between regions.

Each region has an independent DeathStarBench deployment with its own database and cache. A scheduler decision chooses where a new benchmark session runs:

- Run now in the default region.
- Run now in the alternate region.
- Delay a fresh benchmark session until a permitted future time slot.

A benchmark session is a Kubernetes Job that runs the same workload script against the frontend of the selected regional deployment. Each session starts from the same seeded fixture state.

This must be stated in the README and final report as a deliberate simplification: it evaluates carbon-aware placement of fresh workload sessions, not live stateful workload migration.

### Explicitly out of scope

- Cross-region database replication, failover, consistency handling, or live user migration.
- Production-grade multi-cluster routing.
- Physical power measurement.
- Mandatory Kepler deployment or workload energy attribution.
- Formal statistical hypothesis testing and sample-size calculations.
- Broad unit/integration test coverage outside trust-gating and MILP logic.
- Any fabricated carbon, latency, SLO, or emissions result.

AKS is regional, so the two-region experiment requires two clusters. [AKS documentation](https://learn.microsoft.com/en-us/azure/aks/faq)

---

## 2. Repository and implementation structure

Create a Python-based repository with infrastructure and deployment definitions kept separate from controller code.

```text
.
├── README.md
├── pyproject.toml
├── config/
│   ├── experiment.draft.yaml
│   ├── experiment.yaml
│   └── rails/
├── infra/
│   └── bicep/
├── deploy/
│   ├── deathstarbench/
│   ├── controller/
│   ├── neo4j/
│   ├── qdrant/
│   └── observability/
├── src/carbon_scheduler/
│   ├── schemas.py
│   ├── settings.py
│   ├── carbon_client.py
│   ├── graph_ingest.py
│   ├── retrieval.py
│   ├── agent.py
│   ├── trust.py
│   ├── milp.py
│   ├── executor.py
│   ├── workflow.py
│   ├── telemetry.py
│   └── results.py
├── scripts/
│   ├── preflight.py
│   ├── capture_carbon.py
│   ├── seed_deathstarbench.py
│   ├── run_experiment.py
│   ├── collect_results.py
│   ├── summarize_results.py
│   ├── start_azure.ps1
│   └── stop_azure.ps1
├── tests/
│   ├── test_trust.py
│   └── test_milp.py
├── data/
│   └── carbon_cache/
└── artifacts/
    └── runs/
```

Use Python 3.11--3.13. The checked-in `pyproject.toml` bounds the core and
optional dependency versions, but this repository does not claim a universal
lockfile: optional cloud/agent packages are resolved in the operator's
environment. Record the installed package set (`pip freeze`) with any real
run if exact environment reproduction is needed.

Do not add a database solely for experiment metadata. Persist run logs as JSONL, raw load-test output, YAML manifests, and generated CSV files under `artifacts/runs/<run-id>/`.

---

## 3. Required configuration

Create `config/experiment.draft.yaml` with required fields but no invented thresholds or measured values.

The implementation must refuse main experiment execution while required fields remain unset.

Required configuration groups:

```yaml
azure:
  resource_group:
  primary_region:
  secondary_region:
  primary_cluster:
  secondary_cluster:

carbon:
  api_base_url:
  provider: azure
  primary_provider_region:
  secondary_provider_region:
  forecast_horizon_hours:
  cache_directory:
  cache_max_age_seconds:
  replay_trace_path:

workload:
  namespace:
  frontend_service:
  benchmark_script:
  profile_id:
  earliest_start:
  deadline:
  reset_before_session: true

slo:
  profile_p95_limit_ms:
  profile_error_rate_limit:

trust:
  threshold:
  weights:
    ragas_faithfulness:
    ragas_context_precision:
    nemo_rails:
    data_freshness:
    execution_feasibility:

milp:
  slot_minutes:
  allowed_regions:
  latency_catalog_path:

ollama:
  base_url:
  model:
  request_timeout_seconds:
  retry_count:

experiments:
  modes:
    - llm_only
    - milp_only
    - hybrid
  static_reference_region:
  random_seed:
```

After a small pilot, copy the draft to `experiment.yaml`, fill only values produced or chosen during setup, and hash the completed file into every run manifest.

---

## 4. Azure and Kubernetes implementation

### 4.1 Preflight before provisioning

Implement `scripts/preflight.py` to verify:

- Azure subscription is active.
- Requested Azure regions and VM SKUs are available to the student subscription.
- Budget alerts at 50%, 80%, and 100% are configured.
- Electricity Maps credentials can query both selected Azure provider-region names.
- The local laptop can reach Ollama.
- The AKS-side controller can reach Ollama through Tailscale.
- The two-region deployment fits the available quota.
- Docker/Helm manifests can be rendered locally before cloud deployment.

Do not provision clusters until all preflight checks pass.

### 4.2 Azure resources

Use Bicep to create:

- One resource group.
- One AKS cluster per selected region.
- Minimal node pools appropriate for the DeathStarBench smoke deployment.
- Required networking and public/private endpoint settings needed for cluster management.
- Azure cost tags on all resources.

Use a start/stop script rather than leaving clusters active. The stop script must collect results first, then stop both clusters. It must not delete resources automatically.

### 4.3 Regional deployments

Deploy the following to both regional clusters:

- DeathStarBench Social Network.
- Its required databases and caches.
- A regional benchmark namespace.
- A reset/reseed Job.
- A benchmark Job template.
- Minimal monitoring needed for controller-visible status.

Deploy the following only in the primary/controller cluster:

- Controller deployment.
- Neo4j.
- Qdrant.
- OpenTelemetry Collector.
- Jaeger.
- Controller configuration and secrets.

The controller uses its in-cluster service account for the local target and a
separately mounted, least-privilege kubeconfig/context for the other target.
The runner rejects a configuration that marks both independent regions as
`in-cluster`, because that could dispatch both sessions to one cluster.

### 4.4 DeathStarBench seed/reset behavior

Implement a repeatable reset script for each regional stamp:

1. Clear the application’s local benchmark data.
2. Initialize the same Social Network fixture data.
3. Verify the frontend health endpoint.
4. Record the reset completion timestamp.
5. Refuse to start a benchmark session if reset or health verification fails.

The reset script is required because regions are independent and mixed read/write traffic changes application state.

---

## 5. Carbon data and cache implementation

### 5.1 Live capture mode

Implement `carbon_client.py` with an Electricity Maps client that retrieves, for both configured Azure provider-regions:

- Latest carbon intensity.
- Forecast carbon intensity.
- Historical data when needed for setup or analysis.
- Optional power-source breakdown for retrieval context.

Every live response must be saved verbatim in the region cache record with:

- Region query identifiers.
- Request timestamp.
- Response timestamp.
- HTTP status.
- Data source name.
- API response body.
- Query mode: latest, forecast, or historical.

Use Electricity Maps provider-region parameters rather than manually mapping Azure regions to electricity zones. [Electricity Maps API reference](https://app.electricitymaps.com/developer-hub/api/reference)

### 5.2 Cache fallback

For every successful live response:

- Save an immutable JSON snapshot under `data/carbon_cache/`.
- Update a region-specific “latest valid snapshot” pointer.

If a live query fails because of timeout, rate limiting, or service error:

- Load the latest cache snapshot for that exact configured provider-region.
- Mark the cycle as `source=cache`.
- Record the live failure reason.
- Refuse the decision if no cache entry exists or if the cache exceeds configured maximum age.

Never fabricate a carbon value when both live and cache data are unavailable.

### 5.3 Replay mode

Main comparisons use replay mode:

1. Capture a real carbon trace with authenticated live calls.
2. Freeze the trace file and record its hash.
3. Feed the same trace to LLM-only, MILP-only, hybrid, and static-reference sessions.
4. Do not call the live API during replay.

This is necessary for fair sequential comparison. Live demonstrations may be run separately but must not be combined with replay comparison results.

---

## 6. Dependency graph and retrieval context

### 6.1 Neo4j graph

Implement `graph_ingest.py` to parse DeathStarBench deployment/manifests and create:

- A node for each application service, database, cache, and frontend.
- An edge for each declared manifest dependency.
- Metadata containing manifest path, source type, and extraction timestamp.

Label these as manifest-derived dependencies, not verified runtime call traces.

The local runner reads the frozen graph JSON for each cycle. The ingestion
script can optionally mirror the same graph to Neo4j for inspection, but the
prototype does not depend on a runtime Neo4j query to make a decision. The
graph context supplied to a scheduler cycle returns:

- Requested frontend/workload service.
- Its declared dependencies.
- Regional availability.
- Related service names allowed in agent actions.

### 6.2 Qdrant context

Use Qdrant for short, structured context records:

- Carbon snapshot summaries.
- Dependency graph summaries.
- Current experiment policy.
- Prior controller events from the same or earlier timestamp.
- Known failure events, such as tunnel or cache failures.

Every Qdrant record must contain:

- `run_id`
- `event_time`
- `source_type`
- `region`
- `content`
- `content_hash`

Retrieval must filter out records created after the current decision timestamp. This prevents future experiment outcomes from leaking into an earlier decision.

---

## 7. Controller data contracts

Define Pydantic models in `schemas.py`. Persist every model as JSON in the run log.

### 7.1 `CarbonSnapshot`

Contains:

- Snapshot ID.
- Capture/replay mode.
- Decision timestamp.
- Per-region current carbon intensity.
- Per-region forecast series.
- Source status: live or cache.
- Raw source file path/hash.
- Freshness metadata.

### 7.2 `DecisionInput`

Contains:

- Run ID and cycle ID.
- Selected workload profile.
- Earliest start and deadline.
- Candidate regions.
- Carbon snapshot.
- Neo4j dependency context.
- Qdrant context IDs.
- SLO limits.
- Kubernetes availability status.
- Completed experiment configuration hash.

### 7.3 `ActionProposal`

Contains:

- Action type: `run_now`, `run_in_region`, or `delay_until`.
- Target region.
- Scheduled time.
- Workload profile.
- Expected p95 latency.
- Self-reported confidence.
- Evidence IDs.
- Structured rationale.
- Model name, prompt version, and request ID.

### 7.4 `TrustReport`

Contains:

- RAGAS faithfulness.
- RAGAS context precision.
- NeMo rail results.
- Data freshness score.
- Execution feasibility score.
- Weighted trust score.
- Threshold.
- Pass/fail status.
- Failure reason when unavailable or invalid.

### 7.5 `ExecutionReceipt`

Contains:

- Selected algorithm mode.
- Final action.
- Fallback trigger status.
- Kubernetes cluster and Job name.
- Job start/end time.
- Benchmark artifact paths.
- Actual p95 latency.
- Error rate.
- SLO pass/fail.
- Execution failure reason, if any.

---

## 8. LangGraph agent flow

Implement one LangGraph workflow with these nodes:

1. `collect_context`
2. `propose_action`
3. `evaluate_trust`
4. `choose_final_action`
5. `execute`
6. `record_result`

### 8.1 Context collection

The runner loads the frozen replay snapshot, reads the manifest-derived graph
JSON, upserts/retrieves timestamp-filtered context (in-memory locally or
Qdrant in the controller deployment), and builds `DecisionInput`. Kubernetes
reachability is checked by the executor immediately before reset/benchmark Job
creation; a failed check is recorded as an execution failure.

### 8.2 Proposal generation

The Ollama-backed LLM receives only:

- Current workload/session information.
- Current and forecast carbon data.
- Allowed regions and time slots.
- SLO constraints.
- Manifest-derived service context.
- Retrieved operational context.
- Required JSON schema.

Reject prose output, missing fields, unknown regions, unknown services, or invalid timestamps.

### 8.3 RAGAS and NeMo evaluation

Run RAGAS on every decision cycle as required:

- Faithfulness: whether the proposal rationale is supported by the retrieved context.
- Context precision: compare proposal/retrieval against a deterministic canonical policy record generated from the frozen `DecisionInput`.

The canonical policy record must state only:

- Permitted action types.
- Candidate regions.
- Current carbon values.
- Forecast values.
- Deadline.
- SLO.
- Available services.
- Required action fields.

Apply the deterministic Python rails after parsing the structured proposal. The
optional `nemoguardrails` package is installable for future policy integration,
but this prototype does not treat an unavailable NeMo runtime as permission to
invent a score; the deterministic checks remain the safety authority:

- Target service exists in the graph.
- Target region is configured and reachable.
- Proposal contains latency estimate.
- Proposal respects earliest-start/deadline boundaries.
- Proposal matches the JSON action schema.

If RAGAS, Ollama, the tunnel, or the deterministic rail evaluation fails,
create a failed `TrustReport`; do not substitute invented scores. If a future
NeMo runtime is enabled, its failure must use the same fail-closed path.

### 8.4 Trust calculation

Use normalized configured values:

```text
trust_score =
  faithfulness_weight × ragas_faithfulness +
  context_precision_weight × ragas_context_precision +
  rails_weight × rails_score +
  freshness_weight × data_freshness_score +
  feasibility_weight × execution_feasibility_score
```

Rules:

- Weights must sum to one.
- Each component must be in the range zero to one.
- Missing evaluator output fails the hybrid trust gate.
- Self-reported LLM confidence is logged but excluded from the trust formula.
- Threshold and weights are read only from `experiment.yaml`.

---

## 9. MILP fallback

Implement PuLP in `milp.py`.

### 9.1 Decision variables

For each feasible region and allowed time slot:

```text
x[region, slot] = 1 if the session runs in that region at that slot
```

### 9.2 Objective

Without Kepler:

```text
minimize selected carbon intensity
```

This must be labeled as carbon-intensity-aware placement, not emissions minimization.

If the optional Kepler phase succeeds:

```text
minimize estimated workload energy × selected carbon intensity
```

This must be labeled estimated operational emissions.

### 9.3 Constraints

- Exactly one region/time choice is selected.
- Start time is not before earliest start.
- Start time is not after deadline.
- Region is configured and available.
- The pilot latency catalog marks that region/profile as SLO-feasible.
- The benchmark reset step can complete before execution.

The latency catalog is a simple pilot-derived table keyed by workload profile and region. Do not introduce a predictive ML latency model.

### 9.4 Infeasible result

If no region/time combination satisfies constraints:

- Return `infeasible`.
- Create no benchmark Job.
- Log the reason.
- Do not silently execute a default region.

---

## 10. Kubernetes execution behavior

Implement `executor.py` with these responsibilities:

1. Validate final action against configured allowed regions.
2. Run the target region’s reset/reseed Job.
3. Wait for frontend health.
4. Create the regional benchmark Job with:
   - Run ID.
   - Cycle ID.
   - Algorithm mode.
   - Target region.
   - Carbon snapshot ID.
   - Configuration hash.
5. Wait for completion or timeout.
6. Collect wrk2 output and Kubernetes Job status.
7. Create `ExecutionReceipt`.

The executor must be idempotent for a cycle ID. Retrying a controller request must not create duplicate benchmark Jobs.

---

## 11. Experiment execution

### 11.1 Pilot

Run only enough pilot sessions to determine:

- Which workload profile runs successfully in both regions.
- Real p95 latency for each region/profile combination.
- Error rate under each profile.
- Valid SLO and error limits.
- Feasible deadline/delay settings.
- Whether the clusters remain stable at the chosen workload rate.

Record the selected values in `experiment.yaml`. Do not claim pilot values as final research outcomes.

### 11.2 Main configurations

Run these three modes:

| Mode | Agent proposal | Trust gate | MILP |
|---|---:|---:|---:|
| LLM-only | Yes | No | No |
| MILP-only | No | No | Yes |
| Hybrid | Yes | Yes | Only on low trust/failure |

Keep executor safety validation in all modes.

For LLM-only:

- Valid proposals execute directly.
- Invalid/unavailable proposals produce a logged no-action outcome.

For hybrid:

- Low trust, invalid proposals, RAGAS failures, NeMo failures, or LLM tunnel failures trigger PuLP.
- If PuLP is infeasible, record an infeasible outcome.

### 11.3 Static reference

Run a simple static reference policy:

```text
always run immediately in the configured default region
```

Use it only as the denominator for carbon-intensity-aware placement percentage differences. It is not a fourth primary algorithm.

### 11.4 Carbon comparison

Without Kepler, calculate only:

```text
carbon-intensity difference =
  chosen carbon intensity - static-reference carbon intensity
```

```text
carbon-intensity reduction percentage =
  (static-reference intensity - chosen intensity)
  / static-reference intensity × 100
```

Do not call this emissions reduction.

With Kepler, additionally calculate estimated operational emissions from logged energy estimates and carbon intensity. Keep this as a separate, clearly labeled optional result.

---

## 12. Results and reporting

For every configuration, generate:

- Raw JSONL controller cycle logs.
- Raw wrk2 output.
- CSV summary.
- Carbon-intensity-aware placement comparison table.
- p95 latency table.
- Error/SLO-violation table.
- Trust-score distribution plot.
- Fallback count and percentage.
- Live versus cache-fallback count.
- Configuration, input trace, model, prompt, and graph hashes.

Use simple descriptive reporting:

- Raw values.
- Mean/median where appropriate.
- Percentage differences versus static reference.
- Counts of accepted, rejected, fallback, and infeasible decisions.

Do not add bootstrap, permutation, confidence intervals, or p-values unless explicitly requested later.

The README must include:

- Local prerequisites.
- Azure provisioning steps.
- Tailscale and Ollama setup.
- Electricity Maps setup.
- Capture mode.
- Replay mode.
- Running each configuration.
- Result collection and plot generation.
- Teardown procedure.
- Limitations.

---

## 13. Focused verification

Write automated tests only for the novel logic.

### Trust tests

Cover:

- All component scores combine correctly.
- Weights must sum to one.
- Missing RAGAS score fails hybrid gating.
- Failed NeMo rail fails hybrid gating.
- Threshold pass executes proposal.
- Threshold failure selects MILP fallback.
- Invalid LLM JSON cannot execute.

### MILP tests

Cover:

- Lowest valid carbon-intensity option is selected.
- Deadline removes invalid future slots.
- SLO catalog removes unsafe regions.
- No feasible option returns `infeasible`.
- Tie behavior is deterministic.

Use test-only inputs exclusively for algorithm validation. Never place synthetic test values into experiment artifacts, reports, README result sections, or submitted datasets.

### Manual cloud smoke checks

Before main experiments, verify:

- Both DeathStarBench regional deployments respond.
- Both reset/reseed Jobs work.
- Live Electricity Maps calls work.
- Forced cache fallback works.
- The controller reaches Ollama through Tailscale.
- A valid agent proposal creates a benchmark Job.
- A forced low-trust proposal invokes MILP.
- Results can be collected before stopping AKS.

---

## 14. Delivery order

1. Create repository, configuration validation, dependency lockfile, and README skeleton.
2. Build local controller schemas, carbon cache client, agent JSON parser, trust module, MILP module, and focused tests.
3. Build DeathStarBench deployment/reset/benchmark manifests and graph ingestion script.
4. Add Neo4j, Qdrant, OpenTelemetry, and controller deployment manifests.
5. Create Bicep and cost-control scripts.
6. Run Azure preflight and deploy two clusters.
7. Validate live carbon capture, Tailscale/Ollama connectivity, and regional benchmark Jobs.
8. Run pilot and freeze `experiment.yaml`.
9. Capture a real carbon trace.
10. Run replayed LLM-only, MILP-only, hybrid, and static-reference sessions.
11. Collect artifacts, generate CSV/plots, and write only findings traceable to raw run data.
12. Optionally attempt Kepler only after all core deliverables work.

## 15. Final acceptance criteria

The project is complete when:

- Two independent regional DeathStarBench deployments run on AKS.
- A controller can dispatch a fresh benchmark session to either region or delay it.
- Electricity Maps live data and cache fallback both work.
- LLM-only, MILP-only, and hybrid paths all execute.
- Hybrid low-trust behavior demonstrably invokes MILP.
- Trust and MILP unit tests pass.
- Every reported result links to raw logs from a real run.
- The README explicitly discloses independent regional state, fresh-session switching, local-LLM tunnel limitations, small-scale infrastructure, and carbon-intensity-only fallback reporting.
