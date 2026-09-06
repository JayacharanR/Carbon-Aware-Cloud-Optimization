# Guardrail configuration

The controller applies these deterministic requirements before a Kubernetes
action can run:

- the target service appears in the manifest-derived dependency graph;
- the target region is configured and reachable;
- the proposal contains a latency estimate;
- the expected p95 latency does not exceed the configured SLO;
- the scheduled time is within the configured session window; and
- the proposal matches the structured action schema.

NeMo Guardrails is an optional runtime integration. The core validation rules
remain available without its Python package so a missing optional integration
cannot silently make an unsafe proposal executable.
