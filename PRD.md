# Project: Trust-Gated Hybrid-RAG Agentic Framework for Carbon-Aware Cloud Scheduling

## Context
Build a research prototype that tests whether gating an LLM agent's cloud-
scheduling decisions behind a quantitative trust score — falling back to a
deterministic MILP solver when trust is low — improves SLO compliance and
carbon savings versus an unconstrained LLM agent alone. Deployed on real
Azure infrastructure (per faculty requirement), using real carbon-intensity
data and a real open-source microservice application.

Report only real, measured numbers from actual runs. Do not fabricate or
estimate metrics anywhere, including in comments, docstrings, or the final
write-up. Explicitly document every simplification/limitation (listed below)
rather than silently working around them.

## Core Research Question
Does trust-gated hybrid decision-making (LLM agent + guardrail + MILP
fallback) outperform both (a) an unconstrained LLM agent and (b) a MILP-only
baseline, on:
1. Carbon emissions reduction (vs. a static "always-now, always-local" baseline)
2. SLO (latency) violation rate, measured with real load testing
3. Fallback trigger frequency (sanity check that the guardrail is doing real work)

## Sustainable Development Goals (for reporting/context only, not implementation)
- SDG 13 (Climate Action) — primary
- SDG 9 (Industry, Innovation & Infrastructure) — secondary
- SDG 12 (Responsible Consumption & Production) — secondary

## Cloud Provider & Infrastructure
- **Provider:** Azure, using Azure for Students credit ($100, no credit card)
- **Kubernetes:** AKS (Azure Kubernetes Service) — use the Free tier control
  plane (no control-plane cost on Azure, unlike EKS)
- **Node pools:** 2-3 small burstable VMs (B2s-class), sized for a research
  prototype, not production load
- **Regions:** deploy across exactly 2 real Azure regions with confirmed
  Electricity Maps coverage — recommended: **West US 2** and **East US**
  (confirm both are queryable via Electricity Maps' Azure-provider lookup
  before committing to these specific two)
- **Resource Group:** one resource group holding everything, for clean
  cost tracking and teardown
- **Cost management (critical):**
  - Set a billing budget alert immediately at account creation (50%/80%/100%
    thresholds)
  - Tear down or scale down the AKS cluster between active work sessions —
    do not leave it running 24/7
  - Prefer spot/low-priority VMs for worker nodes where feasible

## LLM Backend
- **Model:** llama3:8b via Ollama, running locally on the developer's own
  RTX 4060 laptop (NOT deployed in Azure — this avoids GPU-instance costs
  entirely)
- **Connectivity:** Azure-side controller reaches the local Ollama instance
  over a Tailscale tunnel (or equivalent secure tunnel) — the laptop must
  remain on and reachable during all experiment runs
- Build in a retry/timeout mechanism for the tunnel call, and log any
  connectivity failures distinctly from genuine LLM/agent errors

## Datasets & External Data Sources

### 1. Electricity Maps API (carbon intensity — primary and only carbon data source)
- Use the **14-day full-access trial** (5-min granularity, real-time +
  historical + 24-72h forecast, all signals, no credit card)
- Use the **Data center → Provider: azure → Region** query mode specifically
  — this maps directly to Azure region names, removing any need for manual
  grid-to-region mapping
- Query both chosen Azure regions (West US 2, East US) for:
  - Latest carbon intensity
  - Historical carbon intensity (as much of the trial window as available)
  - 24-72h forecast carbon intensity
  - Power source breakdown (optional but valuable — use as extra context
    text for the vector-search/RAG layer, e.g. "grid currently 60% gas, 20%
    solar")
- **Live Data with Cache Fallback:** The system must always fetch live carbon-intensity
  data from the Electricity Maps API for real-time scheduling decisions and experiments.
  Maintain a local cache (historical + forecast snapshots in CSV/JSON or a local DB)
  as a resilient fallback whenever live API requests fail, time out, or encounter rate
  limits, ensuring continuous operation and offline testability.
- Do NOT use WattTime — dropped from this plan specifically because mixing
  WattTime's marginal-emissions methodology with Electricity Maps' average-
  consumption methodology across two regions would be an inconsistent
  comparison. Using Electricity Maps alone for both regions keeps the
  carbon methodology consistent.

### 2. DeathStarBench (Social Network application)
- Sole workload dataset and benchmark application
- Deploy the real, open-source microservice application on the AKS cluster
- Extract its actual inter-service dependency structure (from its
  docker-compose/K8s manifests) — this becomes the real dependency graph,
  not a synthetic one
- Workload demand patterns, service flexibility, and traffic generation are
  driven directly by DeathStarBench via wrk/hey load testing against its front-end services

## Tool Stack (final)

| Role | Tool | Where it runs |
|---|---|---|
| Container orchestration | AKS (Free tier) | Azure |
| Dependency graph | Neo4j | In-cluster (Azure) |
| Context/log retrieval | Qdrant | In-cluster (Azure) |
| Agent orchestration | LangGraph (3-node flow) | In-cluster controller |
| LLM | Ollama / llama3:8b | Developer's laptop, via Tailscale tunnel |
| Guardrails | RAGAS (faithfulness, context precision) + NeMo Guardrails (rule-based rails) | In-cluster controller |
| Fallback solver | PuLP (MILP) | In-cluster controller |
| Carbon data | Electricity Maps API (Azure-region query mode) | External call from controller |
| Tracing | OpenTelemetry SDK → OTel Collector → Jaeger UI | In-cluster |
| Load generation | wrk or hey | In-cluster job or external script |
| Logging/analysis | JSON/CSV per-cycle logs → pandas | Local analysis, post-run |

## Architecture

Developer laptop (Ollama/llama3:8b)
│ Tailscale tunnel
▼
Azure Resource Group
└── AKS Cluster (Free tier control plane, 2-3 B2s worker nodes)
├── DeathStarBench (real pods, real inter-service traffic)
├── Neo4j (dependency graph, built from DeathStarBench manifests)
├── Qdrant (log/context snippets for retrieval)
├── Controller pod:
│ - LangGraph agent flow (Profiler → Predictor → Action),
│ calls Ollama over the tunnel
│ - RAGAS + NeMo Guardrails trust scoring
│ - PuLP MILP fallback solver
│ - Kubernetes API client — actually reschedules/relabels
│ real pods based on the chosen action
└── OTel Collector + Jaeger (traces every stage)
│
▼
Electricity Maps API
(live queries by Azure region: West US 2, East US —
local cache used as fallback)


## Decision-Cycle Flow (per timestep)
1. Fetch live carbon intensity for both regions via Electricity Maps API
   (falling back to local cache if the API request fails, times out, or is rate-limited)
2. Query Neo4j for the target workload's dependency context (Cypher)
3. Query Qdrant for relevant log/context snippets
4. Merge into one context payload, pass to the LangGraph agent flow:
   - **Profiler node:** determine workload flexibility (delayable? movable?
     hard constraints?)
   - **Predictor node:** given carbon data (current + forecast), predict
     best timing/region
   - **Action node:** propose one concrete action (keep / delay N minutes /
     move to region X), with a self-reported confidence score (0-1);
     output must be structured JSON, not free text
5. Score the proposal:
   - RAGAS: faithfulness + context precision against retrieved context
   - NeMo Guardrails rules (minimum 2): (a) action must reference a real
     service present in the Neo4j graph, (b) action must include a latency
     estimate
   - Combine into a single weighted trust score (make weights configurable
     in one config file, not hardcoded across the codebase)
6. **If trust_score >= threshold:** execute via the Kubernetes API — actually
   relabel/reschedule the relevant pod(s)
   **If trust_score < threshold:** solve via PuLP MILP (minimize carbon
   subject to latency <= SLO, using the same carbon data and the
   dependency graph's latency model), then execute that decision instead
7. Measure real resulting latency via wrk/hey against DeathStarBench's
   front-end
8. Wrap every stage above in an OpenTelemetry span, exported to Jaeger
9. Log to CSV/JSON: timestep, workload, proposed action, individual trust
   score components, whether fallback was triggered, resulting real
   latency, SLO violation (y/n), carbon estimate for the choice made

## Experiment Design
Run evaluations under three configurations on the same cluster to keep
comparisons fair:
1. Unconstrained LLM agent only (no guardrail gate — always executes the
   agent's proposed action)
2. MILP-only (no LLM agent involved at all)
3. Trust-gated hybrid (the full pipeline described above)

All configurations operate against live Electricity Maps carbon data (with the
local cache available as a fallback) and identical DeathStarBench load test
profiles. If running controlled offline benchmarks or managing Azure compute
costs, the cached dataset can also be replayed identically across all three
configurations.

## Deliverables
1. Working repo: Azure/AKS setup scripts or IaC (Bicep/Terraform if time
   permits, otherwise documented manual steps), Neo4j/Qdrant ingestion
   scripts, LangGraph agent pipeline, guardrail scoring module, PuLP
   fallback module, Kubernetes controller, OpenTelemetry instrumentation,
   Electricity Maps live client with local cache fallback mechanism, experiment
   runner for all 3 configurations
2. Electricity Maps fallback dataset checked into the repo or documented
   storage location, ensuring results remain reproducible and fallback is
   functional even offline or if API limits are reached
3. Results: CSV/JSON logs for all 3 configurations, comparison plots
   (carbon over time, SLO violations, trust score distribution, fallback
   trigger rate)
4. README covering: setup steps, how to run with live data and verify fallback
   caching, and a clearly labeled "Limitations" section including:
   - Single-region-pair scope (2 Azure regions, not global)
   - Live Electricity Maps API dependency, relying on cache fallback during
     API rate limiting or connectivity hiccups
   - Local LLM via tunnel — any latency this tunnel adds is a confound in
     timing measurements, worth calling out explicitly
   - Small-scale AKS deployment (B2s nodes, DeathStarBench's Social Network
     app only) — not representative of hyperscale production traffic

## Constraints & Non-Negotiables
- No fabricated metrics anywhere — every number in the final report must
  trace back to an actual logged run
- Keep AKS cluster torn down/scaled to zero when not actively running
  experiments, to protect the Azure student credit
- All three experiment configurations must run against identical carbon data
  streams (live or synchronized fallback cache) and identical DeathStarBench
  workload profiles for a fair comparison
- Document every simplification (listed in Deliverables above) rather than
  omitting it