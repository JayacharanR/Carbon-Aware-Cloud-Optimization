# Operational scripts

On Arch Linux, install and run the project through `uv`:

```bash
bash scripts/install-prerequisites-arch.sh --install-uv
uv sync --all-extras
uv run pytest -q
```

`uv.lock` is committed. Do not install project dependencies into the system
Python with `pip`; use `uv sync` and `uv run` instead.

All cloud-mutating scripts require an explicit `-Apply` switch or operate only on the two clusters named in their arguments. They never create a cluster from incomplete configuration or delete Azure resources.

Suggested order:

1. Copy `.env.example` to `.env` and fill it locally. Python scripts and the PowerShell scripts load this file automatically. Do not put secrets in a committed file.
2. Run `uv run python scripts/bootstrap.py --phase infra` to render private Bicep parameter files. Create the subscription budget with `pwsh -File scripts/deploy-budget.ps1 -Apply`, then run `pwsh -File scripts/azure-preflight.ps1`.
3. Review `deploy-aks.ps1` without `-Apply`; after the what-if and budget pass, re-run with `-Apply`.
4. Start both clusters with `start-azure.ps1`, build/push the controller and `dsb-tools` images, then apply the controller-side platform with `deploy-platform.ps1 -Apply`.
5. Check out DeathStarBench at an immutable SHA with its submodules initialized. Run `install-deathstarbench.ps1 -Apply` once per regional context, using separate rendered-manifest artifact paths.
6. Smoke-test seeding, benchmark execution, live carbon retrieval, cache fallback, and the controller's secure Ollama endpoint before a pilot.
7. Use `stop-azure.ps1` with the real run ID and artifact directory after collection. `-SkipCollection` is an explicit escape hatch, not the normal workflow.

`azure-preflight.ps1` verifies local tooling, the active Azure subscription, presence of a subscription budget, region/SKU signals, a local Ollama endpoint, and static manifest rendering. It cannot prove live Azure capacity, quota, the future availability of a spot VM, or Electricity Maps provider-region coverage. The Python configuration/carbon preflight must make those authenticated, experiment-specific checks before provisioning or running the main study.

After the pilot has produced measured p95/error values, run
`uv run python scripts/bootstrap.py --phase experiment` to render the completed
experiment, target, and latency files. The bootstrapper is deliberately strict:
it does not invent endpoints, workload rates, SLOs, trust weights, or latency
measurements.

## Python commands

All Python commands are safe to run locally after installing the editable
package. They accept repository-relative paths and refuse incomplete
configuration.

```bash
uv run python scripts/preflight.py --config config/experiment.yaml --skip-azure --skip-live --skip-ollama
uv run python scripts/ingest_graph.py --manifests <pinned-deathstarbench-manifests> --output artifacts/graph.json
# Optional in-cluster write; credentials stay in the environment.
uv run python scripts/ingest_graph.py --manifests <pinned-deathstarbench-manifests> --output artifacts/graph.json --neo4j-uri bolt://<neo4j-host>:7687
uv run python scripts/capture_carbon.py --config config/experiment.yaml --output artifacts/carbon-trace.json
uv run python scripts/run_experiment.py --config config/experiment.yaml --mode hybrid --replay-trace artifacts/carbon-trace.json --graph artifacts/graph.json --target-config config/targets.yaml
uv run python scripts/summarize_results.py --runs-dir artifacts/runs
uv run python scripts/plot_results.py --runs-dir artifacts/runs --output-dir artifacts/plots
```

`--dry-run --demo-agent` is available for controller plumbing only. Its
artifacts are marked `demo: true`, contain no workload measurements, and are
excluded by the summary command unless `--include-demo` is supplied.

The target YAML passed to `run_experiment.py` must map each configured Azure
region to `cluster_name`, `kube_context`, `namespace`, `frontend_service`,
`reset_job_template`, `benchmark_job_template`, and a `template_values` map.
It may also set `kubeconfig_path`; use the literal `in-cluster` context for the
controller's local AKS cluster and a separately mounted least-privilege file
for the other cluster.
The template map supplies the pinned `DSB_TOOLS_IMAGE`, benchmark URL/rate,
duration, and resource requests. Leaving a required template value blank is an
execution error; the controller does not guess workload parameters.
