# Scheduler controller deployment

Build the controller image from the repository root and push it to a registry
reachable by the primary AKS cluster:

```bash
docker build -f deploy/controller/Dockerfile -t <registry>/<image>:<tag> .
docker push <registry>/<image>:<tag>
```

Use `pwsh -File scripts/deploy-platform.ps1 -ControllerImage <registry>/<image>:<tag>` to
apply the deployment and its non-versioned ConfigMaps/Secrets. If applying the
base Kustomization manually, replace `image: carbon-scheduler-controller` in
the rendered Deployment first; the placeholder image is not intended to be
pulled from a registry.

Before applying, create the non-versioned Kubernetes objects required by the deployment:

- `scheduler-experiment-config` ConfigMap from the completed `config/experiment.yaml`.
- `scheduler-secrets` with `electricity-maps-api-key`, `neo4j-username`, `neo4j-password`, `neo4j-auth`, and `qdrant-api-key`. `neo4j-auth` is the Neo4j-required `username/password` form and must be constructed outside version control.
- Optionally, `scheduler-cluster-credentials` with a least-privilege kubeconfig for the secondary cluster.

`scripts/deploy-platform.ps1` creates the first two from environment variables and a supplied completed experiment configuration. It does not accept values from this repository as credentials.

The base deployment is intentionally dormant: `EXPERIMENT_MODE`, replay-trace,
graph, latency-catalog, and target-config paths are empty until a concrete run
is reviewed. To configure one replay run, supply all five input paths to the platform script;
it stores them in the `scheduler-run-inputs` ConfigMap and patches the runtime
paths to `/etc/carbon-scheduler/inputs/...`:

```bash
pwsh -File scripts/deploy-platform.ps1 `
  -PrimaryContext <primary-context> -SecondaryContext <secondary-context> `
  -ExperimentConfigPath config\experiment.yaml `
  -ControllerImage <registry>/<image>:<tag> -OllamaBaseUrl <tunnel-url> `
  -ExperimentMode hybrid `
  -ReplayTracePath artifacts\carbon-trace.json `
  -GraphPath artifacts\graph.json `
  -TargetConfigPath config\targets.yaml `
  -LatencyCatalogPath config\latency_catalog.yaml -Apply
```

The ConfigMap is suitable only for a small frozen trace. For a larger trace,
mount an operator-managed volume and set the same runtime paths explicitly.
The target YAML should use `in-cluster` for the controller's local context and
a separately mounted least-privilege kubeconfig for the other cluster.

The deployment selects the six-node LangGraph orchestrator through
`SCHEDULER_ORCHESTRATION_BACKEND=langgraph`. Local commands default to the
same workflow's dependency-light direct implementation unless that variable
is explicitly set to `langgraph`.

The controller's in-cluster service account can create/read only benchmark Jobs in the local `benchmark` namespace. For the other cluster, provide a separately issued least-privilege credential that has the equivalent Role in that cluster; do not mount an administrator kubeconfig. The base manifest has no Tailscale sidecar. Set `OLLAMA_BASE_URL` only to an endpoint already reachable through an operator-managed authenticated tunnel, and verify it during preflight.
