# Plug-and-play setup inputs

This is the operator checklist for filling `.env`. The repository can validate
and render configuration, but it cannot create an Azure account, grant an API
entitlement, choose an experiment workload, or measure pilot latency for you.
Do not send secret values to an agent or commit `.env`.

## 1. Arch Linux local machine (supported path)

After pulling the repository, run this from its root:

```bash
git pull --ff-only
bash scripts/install-prerequisites-arch.sh --install-arch-tools --install-uv
uv sync --all-extras
cp .env.example .env
chmod 600 .env
${EDITOR:-vi} .env
```

`uv.lock` is committed and is the source of truth for Python resolution. Use
`uv run python ...` and `uv run pytest` for every Python command; no manual
activation of `.venv` is required. The helper checks Azure CLI, `kubectl`, Helm,
Docker, Ollama, and Tailscale but intentionally does not guess how those
optional tools are installed on Arch. Install missing tools from their official
Linux instructions and rerun the helper.

The Azure lifecycle files in this repository are PowerShell scripts. Install
PowerShell 7 (`pwsh`) on the Arch host if you want to use those wrappers; the
Python runner and Kubernetes manifests can still be inspected locally without
it.

## 2. Windows local machine

Run this from PowerShell in the repository root:

```powershell
uv sync --all-extras
.\scripts\install-prerequisites.ps1
```

For the real Azure path, install Azure CLI, `kubectl`, Helm, Docker Desktop,
Ollama, and (if using a private laptop route) Tailscale. The explicit helper is
available when Windows Package Manager is installed:

```powershell
.\scripts\install-prerequisites.ps1 -InstallWindowsTools
```

The helper does not create Azure resources. Docker Desktop and Ollama may need
a restart after installation.

If Docker Desktop is the only failed package, check the WSL 2 prerequisite in
an elevated PowerShell and rerun the Docker package interactively:

```powershell
wsl --status
wsl --update
# If WSL is not installed yet (this can require a reboot):
wsl --install --no-distribution
winget install --id Docker.DockerDesktop --exact --interactive `
  --accept-source-agreements --accept-package-agreements
```

Start Docker Desktop after installation and verify `docker version` before
building images. The WinGet package currently uses the machine-scoped Docker
installer, so run that command from an elevated PowerShell if its UAC prompt
does not appear. If you specifically need Docker's per-user mode, download the
official installer and run `Docker Desktop Installer.exe install --user`.

## 3. Azure values

1. Run `az login` in a browser.
2. Use `az account list -o table` to choose a subscription and put its ID in
   `AZURE_SUBSCRIPTION_ID`.
3. Choose two different regions with
   `az account list-locations --query "[].name" -o tsv`; put them in
   `AZURE_PRIMARY_REGION` and `AZURE_SECONDARY_REGION`.
4. Choose globally unique cluster names and DNS prefixes. Use lowercase names
   for the DNS prefixes.
5. Check supported AKS versions with
   `az aks get-versions --location <region> -o table` and set one version that
   is supported in both selected regions.
6. Generate an SSH key if needed with
   `ssh-keygen -t ed25519 -f "$HOME/.ssh/carbon-scheduler"` and set
   `AZURE_SSH_PUBLIC_KEY_PATH` to the `.pub` file.
7. Choose a monthly budget and notification addresses. These are required
   before the AKS deployment; the Bicep deployment does not create an ACR.

Run `uv run python scripts/bootstrap.py --phase infra --check-only` after these fields
are filled. Then create the budget with `pwsh -File scripts/deploy-budget.ps1 -Apply`, run
`pwsh -File scripts/azure-preflight.ps1`, and review the AKS what-if before
`pwsh -File scripts/deploy-aks.ps1 -Apply`.

## 4. Electricity Maps

Create an API token in the Electricity Maps account that has access to the
selected Azure provider regions. Put the token in `ELECTRICITY_MAPS_API_KEY`.
Use the provider-region identifiers shown by that account/API, not arbitrary
Azure geography names, in `CARBON_PRIMARY_PROVIDER_REGION` and
`CARBON_SECONDARY_PROVIDER_REGION`. The two identifiers must be different.

The first live capture verifies the mapping:

```bash
uv run python scripts/preflight.py --config config/experiment.yaml
uv run python scripts/capture_carbon.py --config config/experiment.yaml
```

If the account cannot query both regions, stop there and change the regions or
the Electricity Maps plan. Do not substitute guessed carbon values.

## 5. Ollama

Install Ollama, start it, and pull the exact model used for the study, for
example `ollama pull llama3:8b`. Set `OLLAMA_BASE_URL` and `OLLAMA_MODEL`.
Verify locally with:

```bash
ollama list
curl --fail "$OLLAMA_BASE_URL/api/tags"
```

For an in-cluster controller, the same endpoint must be reachable privately
from AKS for the entire run. Configure the operator-controlled Tailscale,
subnet router, or private proxy separately; do not expose Ollama publicly.

## 6. DeathStarBench and images

Clone the upstream repository, initialize submodules, and record an immutable
commit:

```bash
git clone https://github.com/delimitrou/DeathStarBench.git
cd DeathStarBench
git submodule update --init --recursive
git rev-parse HEAD
```

Set `DEATHSTARBENCH_SOURCE` and `DEATHSTARBENCH_COMMIT` to that checkout and
40-character SHA. Build and push the two images to a registry that both AKS
clusters can pull from:

```bash
docker build -f deploy/controller/Dockerfile -t <registry>/carbon-scheduler-controller:<immutable-tag> .
docker build --build-arg DEATHSTARBENCH_COMMIT=<sha> -f deploy/deathstarbench/loadgen/Dockerfile -t <registry>/dsb-tools:<immutable-tag> .
docker push <registry>/carbon-scheduler-controller:<immutable-tag>
docker push <registry>/dsb-tools:<immutable-tag>
```

Set `CONTROLLER_IMAGE` and `DSB_TOOLS_IMAGE` to the pushed, fully qualified
references. Configure image pull access in both clusters; this repository does
not create a registry or credentials for one.

## 7. Kubernetes target and pilot values

After `az aks get-credentials` for both clusters, list contexts with
`kubectl config get-contexts`. Fill the two target contexts, service names,
internal frontend hosts/ports, and target URLs in `.env`. Run
`pwsh -File scripts/install-deathstarbench.ps1 -Apply` once per context, then run and log the
seed/reset and one manual wrk2 smoke session in each independent cluster.

Use that smoke/pilot output to fill `WORKLOAD_PROFILE_ID`, p95/error SLO limits,
the scheduling window, request rate, threads, connections, duration, graph
dataset path, and the two measured p95 latency values. These are measurements,
not placeholders or defaults. Then run:

```bash
uv run python scripts/bootstrap.py --phase experiment
uv run python scripts/ingest_graph.py --manifests <pinned-rendered-manifests> --output artifacts/graph.json
```

The generated `config/targets.yaml` and `config/latency_catalog.yaml` are now
the inputs for all four comparison arms.

## 8. Platform credentials

Choose local two-context execution, or an in-cluster controller with exactly
one `in-cluster` target and one separately mounted least-privilege kubeconfig.
Never mount an Azure administrator kubeconfig in the controller Pod.

Generate local-only credentials for the platform services in `.env`:

- `NEO4J_USERNAME`/`NEO4J_PASSWORD`: credentials used by the deployed Neo4j
  service;
- `QDRANT_API_KEY`: a locally generated random key used by the deployed Qdrant
  service;
- `NEO4J_URI`, `NEO4J_DATABASE`, and `QDRANT_URL`: service DNS values in
  `scheduler-system` (the example contains repository defaults). For a local
  runner outside AKS, use a deliberate `kubectl port-forward` or another
  authenticated endpoint instead of the in-cluster DNS name.

The platform script creates these Kubernetes Secrets from the current process
environment; it does not write them to source files.

## 9. Final run gate

```bash
uv run python scripts/bootstrap.py --phase experiment --check-only
uv run python scripts/preflight.py --config config/experiment.yaml
uv run python scripts/capture_carbon.py --config config/experiment.yaml
uv run python scripts/run_experiment.py --config config/experiment.yaml --mode llm_only --replay-trace artifacts/carbon-trace.json --graph artifacts/graph.json --target-config config/targets.yaml
```

Repeat the same command for `milp_only`, `hybrid`, and `static_reference` with
the same trace, graph, target file, and latency catalog. Collect raw artifacts
before stopping AKS. The result is a carbon-intensity-aware placement study;
it is not a live failover or measured energy/carbon reduction claim unless the
optional energy phase was completed.
