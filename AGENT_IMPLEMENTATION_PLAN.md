# Agent implementation plan: finish to plug-and-play

This document is the handoff plan for an implementation agent working from a
fresh checkout. It describes what is already in the repository, what the agent
must still implement, what must be supplied by the operator, and the evidence
required before calling the project runnable.

## 1. Fixed scope the agent must preserve

The project evaluates four replayed policies over fresh DeathStarBench Social
Network benchmark sessions:

1. `llm_only`
2. `milp_only`
3. `hybrid` (LLM proposal behind a trust gate, MILP fallback)
4. `static_reference` (denominator only)

The two Azure regions are independent AKS/DeathStarBench stamps. A decision
starts a new reset benchmark Job in one cluster; it does not migrate live pods,
users, sessions, or database state. The scheduler reports carbon-intensity-aware
placement unless an optional energy-measurement phase is completed. Kepler,
formal hypothesis tests, full cloud acceptance coverage, and replicated
cross-region failover are not blockers.

## 2. Current implementation inventory

Already implemented and tested:

- Pydantic contracts and strict completed-configuration validation.
- Electricity Maps latest/forecast client, immutable same-region cache fallback,
  replay trace loading, and source/freshness metadata.
- Structured Ollama proposal agent with bounded retries and invalid-response
  rejection.
- Deterministic rails and fail-closed trust calculation.
- PuLP MILP with deterministic tie-breaking and infeasibility results.
- Direct workflow and optional LangGraph six-node orchestration.
- In-memory and Qdrant retrieval stores.
- Manifest-derived dependency graph JSON and optional Neo4j mirror.
- Kubernetes reset/benchmark Job executor with idempotent cycle names and raw
  Job-log capture.
- Azure Bicep for one resource group and two independent AKS clusters.
- Budget, preflight, start, stop, DeathStarBench installation, result collection,
  summary, and plotting scripts.
- Focused trust, MILP, and workflow tests.
- Controller and regional deployment manifests.

Dependency/reproducibility contract:

- `pyproject.toml` is the package/dependency declaration.
- `uv.lock` is committed and is the authoritative resolved dependency set.
- The supported setup is `uv sync --all-extras`; Python commands are run as
  `uv run ...` so they use the project environment rather than system Python.
- Optional extras remain explicit in the metadata. A minimal local check can
  use `uv sync --extra dev`; the full controller setup uses
  `uv sync --all-extras`.
- The controller image copies `uv.lock` and installs its runtime extras with
  `uv sync --frozen`; it does not resolve a different dependency set at image
  build time.

The repository has intentionally incomplete drafts in `config/`. No real Azure
resources, API trace, workload metrics, or DeathStarBench fixture are stored in
the repository.

## 3. Completed in this pass

### 3.1 Environment loading

- `python-dotenv` is now a core dependency.
- Python entry points load the ignored repository `.env` automatically without
  overriding explicit shell/CI variables.
- PowerShell deployment scripts load `.env` through
  `scripts/load-project-env.ps1`.
- Secret values are never copied to generated YAML, Bicep parameter files, run
  manifests, or stdout.

### 3.2 Configuration bootstrap

`scripts/bootstrap.py` is the single renderer from operator inputs to runtime
files:

```bash
cp .env.example .env
uv run python scripts/bootstrap.py --phase infra
uv run python scripts/bootstrap.py --phase experiment
```

It validates missing/placeholder values, numeric types, trust weights, regions,
timestamps, and pilot latency values before writing:

- `infra/bicep/main.bicepparam`
- `infra/bicep/budget.bicepparam`
- `config/experiment.yaml`
- `config/targets.yaml`
- `config/latency_catalog.yaml`

`--check-only` performs validation without writing. `--force` is required to
overwrite an existing generated file.

### 3.3 Prerequisite installer/checker

`scripts/install-prerequisites.ps1` performs a read-only tool check by default
and requires `uv` for a complete local setup. `-InstallPythonPackages` runs
`uv sync --all-extras`; the explicit
`-InstallWindowsTools` switch may use winget for Azure CLI, kubectl, Helm,
Docker Desktop, Ollama, and Tailscale. A legacy `-UsePipFallback` is available
only when uv cannot be installed. On Arch Linux, use
`scripts/install-prerequisites-arch.sh`; it is read-only by default and has
separate flags for pacman, uv installation, and dependency synchronization.
Neither helper provisions Azure resources.

### 3.4 Dependency compatibility and focused bootstrap tests

- The tested RAGAS 0.4.x import path is compatible with the current
  `langchain-community` layout; its unused Vertex AI import is isolated behind
  a marker-class shim, while any real evaluator/provider failure still fails
  closed to MILP.
- Unit tests cover placeholder detection, Bicep escaping, infrastructure
  validation without runtime credentials, and rendering a complete synthetic
  experiment configuration through the strict Pydantic loader.

## 4. Remaining code work

The following tasks are still required before claiming a turnkey real-cloud
run. An agent should implement them in this order.

### A. Validate the bootstrap path

1. The focused bootstrap tests cover CSV parsing, numeric conversion, region
   overrides, public-key loading, placeholder rejection, missing pilot values,
   invalid trust weights, and fixture-driven rendering.
2. Run the bootstrapper against a temporary `.env` only; never create a real
   `config/experiment.yaml` in source control.

### B. Make the operator workflow explicit

1. Keep all cloud-mutating scripts behind `-Apply`.
2. The budget, AKS, and Azure preflight scripts now derive their ordinary
   paths/values from `.env` when explicit parameters are omitted; keep explicit
   parameters available for CI and multi-project workspaces.
3. Ensure `preflight.py` reports the loaded `.env` path without printing any
   value.
4. Ensure the Python preflight can run in local-only mode before Azure tools are
   installed and in full mode after Azure login.

### C. Image and registry readiness

1. Build the controller image from `deploy/controller/Dockerfile`.
2. Build the `dsb-tools` image with a 40-character DeathStarBench SHA.
3. Require fully qualified image references.
4. Document the manual registry step: both AKS clusters need pull access. The
   Bicep template does not create ACR or image-pull credentials.
5. Record image tags/digests in the run handoff; do not use moving `latest` tags.

### D. Remote Kubernetes credential readiness

1. Local runner: use two operator-selected kubeconfig contexts.
2. In-cluster controller: mark exactly one region `in-cluster`.
3. Mount a separate least-privilege kubeconfig for the other cluster at the
   path named in `TARGET_SECONDARY_KUBECONFIG_PATH`.
4. Ensure that kubeconfig is usable by the Python Kubernetes client inside the
   controller image. The current image does not contain Azure CLI/kubelogin;
   either provide a self-contained certificate/token kubeconfig or explicitly
   extend the image and test that extension.
5. Never mount an Azure administrator kubeconfig in the controller Pod.

### E. Ollama network readiness

1. Keep the local Ollama model and exact model tag/digest in `.env`.
2. Confirm `/api/tags`, `/api/chat`, and `/v1/chat/completions` from the
   controller execution environment.
3. Provide a private route from AKS to the laptop (Tailscale operator, subnet
   router, or equivalent private proxy). The repository does not install this
   route.
4. Do not add an unauthenticated public endpoint. The current client has no
   arbitrary bearer-token configuration; use network-level access control.
5. Keep Ollama reachable for the entire experiment window.

### F. DeathStarBench smoke and reset gate

1. Check out the official repository at an immutable commit with submodules.
2. Render/install the same selected Social Network chart in both clusters.
3. Verify the actual frontend Service name, internal host, port, and target URL.
4. Verify the graph dataset path inside the `dsb-tools` image.
5. Run the reset Job in each region and retain its raw logs.
6. Prove that the selected reset procedure returns both independent database
   fixtures to an equivalent starting state. The supplied seed Job alone does
   not delete existing database state.
7. Run one manual wrk2 smoke session per region and retain raw output.

### G. Pilot and experiment gate

1. Use the smoke output to select one `profile_id`.
2. Measure p95 and error rate in both regions.
3. Choose and record the SLO limits; do not infer them from defaults.
4. Set the scheduling window, slot length, trust threshold, and trust weights.
5. Render `config/experiment.yaml`, `config/targets.yaml`, and
   `config/latency_catalog.yaml` with the bootstrapper.
6. Run Python preflight with live Electricity Maps and Ollama checks.
7. Capture one immutable trace containing exactly the two configured regions.
8. Run all four modes against the same trace, workload profile, graph, target
   file, and latency catalog.
9. Collect artifacts before stopping AKS.

## 5. External inputs the operator must provide

The operator must supply values, access, or local files for:

- Azure subscription, two regions, names, SKU/quota, Kubernetes version, SSH
  public key, budget, and subscription permissions.
- Electricity Maps key and exact Azure provider-region identifiers.
- Ollama model and reachable private endpoint.
- In-cluster platform service values (`NEO4J_URI`, `NEO4J_DATABASE`, and
  `QDRANT_URL`) plus generated Neo4j/Qdrant credentials.
- DeathStarBench source path, immutable commit, selected Lua workload, tools
  image, graph dataset path, service endpoints, and load profile.
- Two Kubernetes contexts or one in-cluster identity plus one self-contained
  remote kubeconfig.
- Pilot p95/error measurements and the chosen SLO.
- Trust weights/threshold, MILP slot duration, replay window, and random seed.
- Controller/tools image references and registry pull permissions.

The operator must not provide secrets to the agent in chat. Set them in `.env`,
the local shell, a secret manager, or Kubernetes Secrets.

`PLUG_AND_PLAY_SETUP.md` gives the exact Azure, Electricity Maps, Ollama,
DeathStarBench, registry, Kubernetes, and pilot-measurement steps used to
obtain those values.

## 6. Acceptance commands

Before infrastructure mutation:

```bash
bash scripts/install-prerequisites-arch.sh
uv run python scripts/bootstrap.py --phase infra --check-only
```

After the pilot:

```bash
uv run python scripts/bootstrap.py --phase experiment --check-only
uv run python scripts/preflight.py --config config/experiment.yaml
uv run pytest -q
```

For a real replay run:

```bash
uv run python scripts/run_experiment.py --config config/experiment.yaml --mode llm_only --replay-trace artifacts/carbon-trace.json --graph artifacts/graph.json --target-config config/targets.yaml --latency-catalog config/latency_catalog.yaml
uv run python scripts/run_experiment.py --config config/experiment.yaml --mode milp_only --replay-trace artifacts/carbon-trace.json --graph artifacts/graph.json --target-config config/targets.yaml --latency-catalog config/latency_catalog.yaml
uv run python scripts/run_experiment.py --config config/experiment.yaml --mode hybrid --replay-trace artifacts/carbon-trace.json --graph artifacts/graph.json --target-config config/targets.yaml --latency-catalog config/latency_catalog.yaml
uv run python scripts/run_experiment.py --config config/experiment.yaml --mode static_reference --replay-trace artifacts/carbon-trace.json --graph artifacts/graph.json --target-config config/targets.yaml --latency-catalog config/latency_catalog.yaml
uv run python scripts/summarize_results.py --runs-dir artifacts/runs
uv run python scripts/plot_results.py --runs-dir artifacts/runs --output-dir artifacts/plots
```

## 7. Definition of plug-and-play

The project is plug-and-play only when:

- `.env` passes bootstrap validation;
- all required local tools and Python extras are installed;
- the two images are pullable by the clusters;
- the regional DSB reset and smoke benchmark pass;
- the Ollama route is reachable from the chosen controller location;
- the pilot-derived latency/SLO catalog is present;
- the same frozen trace is used for every comparison arm; and
- raw artifacts can be collected before AKS is stopped.

No agent can manufacture the Azure account, API entitlements, network route,
registry permissions, DSB endpoint names, or pilot measurements. Those remain
real operator inputs by design.
