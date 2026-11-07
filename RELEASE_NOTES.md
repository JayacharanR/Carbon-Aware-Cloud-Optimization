# Carbon-Aware Cloud Optimization v1.0.0

First versioned release of the trust-gated carbon-aware benchmark-session scheduler.

- Schedules fresh DeathStarBench benchmark sessions across pre-provisioned Azure AKS regions.
- Supports local Llama-3 proposals, deterministic admission checks, and an exact feasible-candidate reference with PuLP/CBC verification.
- Records decisions and input hashes, with optional retrieval, Kubernetes, and OpenTelemetry integrations.
- Publishes the controller container to GitHub Container Registry with versioned tags.

## Container

```bash
docker pull ghcr.io/jayacharanr/carbon-aware-cloud-optimization:1.0.0
```

The image runs the one-shot scheduler controller. Supply completed experiment configuration and runtime input mounts as described in `deploy/controller/README.md`. Live execution requires operator-provided credentials, reachable services, and provisioned clusters. The image does not contain credentials, models, private research, or local experiment outputs.

The scheduler selects benchmark placement and timing. Reported carbon-intensity differences are placement measures; this release does not claim measured reductions in total cluster emissions.

## Validation

The 83 scheduler tests passed against the isolated release source before publication. The publishing workflow repeats the tests, verifies the container version and CLI, and publishes the image after those checks pass. Check the workflow status before pulling the image. GitHub Container Registry initially creates packages as private; authenticated access may be required until visibility is changed in the package settings.
