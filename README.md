# Trust-Gated Carbon-Aware Benchmark Scheduler

This repository is a research prototype for comparing three ways to choose
where and when to run a fresh DeathStarBench Social Network benchmark session:

1. an LLM-only proposal;
2. a deterministic PuLP MILP decision; and
3. a trust-gated LLM proposal that falls back to the MILP when trust is low.

The controller uses Electricity Maps carbon-intensity inputs, a
manifest-derived service dependency graph, retrieval context, structured LLM
proposals, deterministic guardrails, and RAGAS evaluation on every hybrid
cycle. If the RAGAS dependency or evaluator endpoint is unavailable, the
hybrid gate fails closed and records a MILP fallback; it never substitutes a
made-up score.

## Important scope boundary

This prototype does **not** move running pods, databases, users, or sessions
between Azure regions. Each region has an independent DeathStarBench stamp.
A regional choice starts a new, reset benchmark session in the selected
cluster. This is an intentional simplification and must be retained in any
report based on this repository.

Without the optional energy-measurement phase, results are reported as
**carbon-intensity-aware placement differences**, not measured emissions
reductions. The repository contains no fabricated results or default study
thresholds.

## Repository layout

- `src/carbon_scheduler/` — controller logic, data contracts, trust gate,
  fallback optimizer, Kubernetes execution adapter, and result processing.
- `config/` — incomplete experiment template and deterministic rail policy.
- `infra/bicep/` — Azure resource definitions for the two independent AKS
  clusters.
- `deploy/` — Kubernetes manifests for regional workload stamps and controller
  services.
- `scripts/` — preflight, carbon capture, experiment, collection, and Azure
  lifecycle commands.
- `tests/` — focused trust-gate and MILP tests.

## Prerequisites

- Python 3.11–3.13.
- Azure CLI, `kubectl`, and Helm when deploying to Azure.
- An Azure subscription with quota for two small AKS clusters.
- Electricity Maps API credentials with Azure data-centre provider access.
- Docker access for container builds and DeathStarBench images.
- A local Ollama service running the selected model. For Azure execution, make
  it reachable only through the configured secure tunnel.

The core package has a small local dependency set. Kubernetes, observability,
and agent integrations are opt-in extras so local validation does not require
Azure, Ollama, RAGAS, or NeMo Guardrails.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
python -m pip install -e ".[kubernetes,storage,agent,observability]"
```

Install the second command only on the machine that will run the full
controller. Do not put API tokens, kubeconfigs, tunnel keys, or completed
experiment configurations in version control.

## Configure before running

Copy the intentionally incomplete template:

```powershell
Copy-Item config\experiment.draft.yaml config\experiment.yaml
```

After installing the two regional releases and completing the pilot, copy the
target template and fill its per-region cluster credentials and benchmark
template values:

```powershell
Copy-Item config\targets.draft.yaml config\targets.yaml
```

The latency catalog is also pilot output, not a guessed default:

```powershell
Copy-Item config\latency_catalog.draft.yaml config\latency_catalog.yaml
```

Fill `config/experiment.yaml` only after the pilot establishes a real workload
profile, SLO, latency catalog, permitted regions, and scheduling window. The
main experiment command rejects missing values, placeholder values, invalid
trust weights, and regions not listed by the configuration.

Store secrets outside the YAML file:

- `ELECTRICITY_MAPS_API_KEY` — Electricity Maps API token.
- Kubernetes credentials — use kubeconfig contexts or in-cluster service
  accounts, not source-controlled token files.
- Tunnel credentials — provision through the tunnel provider, never in a
  Kubernetes manifest.

## Run order

1. Run preflight checks before provisioning. This verifies local tooling,
   config shape, Azure access where available, Electricity Maps provider-region
   queries, and Ollama reachability.

   ```powershell
   python scripts/preflight.py --config config/experiment.yaml
   ```

2. Provision Azure only after preflight passes. Create budget alerts first.

   ```powershell
   .\scripts\start-azure.ps1 -ResourceGroup <resource-group> -PrimaryCluster <primary-cluster> -SecondaryCluster <secondary-cluster>
   ```

3. Deploy the matching regional DeathStarBench stamps, seed each regional
   fixture, and verify benchmark jobs manually with a smoke run.

4. Run a short pilot. Freeze the resulting SLO values and latency catalog in
   `config/experiment.yaml`; then record its hash in every experiment run.

5. Capture an authenticated live Electricity Maps timeline. The client writes
   the raw provider responses to the configured cache alongside the frozen
   snapshot trace; retain both locations for audit. The frozen trace is the
   only allowed input source for a replay comparison.

   ```powershell
   python scripts/capture_carbon.py --config config/experiment.yaml
   ```

6. Run each mode against the same frozen replay trace. The static default-region
   policy is a reference denominator, not a fourth scheduling algorithm.

   ```powershell
   python scripts/run_experiment.py --config config/experiment.yaml --mode llm_only
   python scripts/run_experiment.py --config config/experiment.yaml --mode milp_only
   python scripts/run_experiment.py --config config/experiment.yaml --mode hybrid
   python scripts/run_experiment.py --config config/experiment.yaml --mode static_reference
   ```

7. Collect raw output and generate descriptive tables/plots from artifact files.

   ```powershell
   python scripts/collect_results.py --run-dir artifacts/runs/<run-id>
   python scripts/summarize_results.py --runs-dir artifacts/runs
   python scripts/plot_results.py --runs-dir artifacts/runs --output-dir artifacts/plots
   ```

   Plotting uses only observed values and requires the optional
   `analysis` extra (`python -m pip install -e ".[analysis]"`). Missing
   measurements produce no point/bar; the helper does not impute values or
   perform formal statistical tests.

   When exactly one non-demo `static_reference` run is present, the summary
   also derives each run's mean carbon-intensity difference and percentage
   difference from that observed reference. If the reference or denominator is
   unavailable, those fields remain empty.

8. Stop AKS after copying durable artifacts.

   ```powershell
   .\scripts\stop-azure.ps1 -ResourceGroup <resource-group> -PrimaryCluster <primary-cluster> -SecondaryCluster <secondary-cluster> -RunId <run-id> -ArtifactDirectory <durable-output>
   ```

## Decision behavior

The agent may return only `run_now`, `run_in_region`, or `delay_until` as
strict structured JSON. Its self-reported confidence is logged but is not used
in the trust calculation. Ollama connectivity/tunnel failures are recorded
with an `llm_unavailable:` reason, while malformed model output is recorded as
`llm_response_invalid:`; neither is silently treated as a successful proposal.

The hybrid trust score combines configured RAGAS, rail, freshness, and
execution-feasibility components. Missing evaluator output or a failed rail
fails closed and invokes the MILP. The MILP selects exactly one feasible
region/time slot or records an infeasible outcome; it never silently runs a
default workload.

The local runner uses its deterministic in-memory retrieval store by default.
Set `SCHEDULER_RETRIEVAL_BACKEND=qdrant`, `QDRANT_URL`, and (when required)
`QDRANT_API_KEY` to use the in-cluster Qdrant adapter. The controller
deployment sets this backend to Qdrant; a missing or unreachable Qdrant
service fails the run rather than silently changing the experiment's retrieval
source.

The local runner uses the dependency-light direct orchestrator by default. The
controller deployment sets `SCHEDULER_ORCHESTRATION_BACKEND=langgraph` and
the image installs the LangGraph extra; if that package is missing, the
controller fails clearly instead of silently changing the orchestration path.
The LangGraph nodes are `collect_context`, `propose_action`, `evaluate_trust`,
`choose_final_action`, `execute`, and `record_result`; they share the same
Pydantic contracts and fail-closed behavior as the direct path.

## Tests

The automated test scope deliberately covers the novel contribution only:
trust-gating and MILP behavior.

```powershell
python -m pytest
```

If `pytest` is unavailable, use the standard-library fallback where provided:

```powershell
python -m unittest discover -s tests
```

## Data integrity and limitations

- Every reported number must be traceable to a raw run artifact.
- A real Kubernetes run stores the raw reset and benchmark Job logs under
  `benchmark-logs/` in its run directory; parser failures leave the observed
  values empty instead of relabeling another percentile.
- Infrastructure failures and dry runs leave SLO status unknown; they are not
  counted as workload violations.
- Live API failure uses only a saved, age-bounded snapshot from the same
  configured provider-region. Missing or stale data blocks the decision.
- Replay mode is required for fair sequential comparison; live demonstrations
  must be reported separately.
- A replayed snapshot that was captured from cache retains its fallback reason;
  summaries label it `cache_fallback_replay` and the hybrid freshness component
  remains conservative.
- The dependency graph is manifest-derived, not a verified runtime call graph.
- The local LLM tunnel can affect control-plane timing and is logged separately
  from workload latency.
- Workload latency is measured during stable benchmark sessions. It does not
  claim to measure cross-region migration latency because no migration exists.
- Kepler/energy attribution is optional. If unavailable, do not use the word
  “emissions” for carbon-intensity-only results.
- The small two-region AKS deployment is not a production-scale cloud
  scheduling system.
