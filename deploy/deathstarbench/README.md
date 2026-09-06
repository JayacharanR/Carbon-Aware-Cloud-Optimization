# DeathStarBench regional workload assets

Each regional cluster receives its own upstream Social Network Helm release in `benchmark`. The deployments and their databases are deliberately independent. A target-region decision starts a fresh benchmark session against that region; it does not migrate application state or live traffic.

The upstream chart path is `socialNetwork/helm-chart/socialnetwork`. `scripts/install-deathstarbench.ps1` requires an immutable 40-character DeathStarBench commit, so the rendered manifest and workload source are traceable. It uses the upstream `compose-post.lua` workload by default and records the chart rendering for manifest-derived graph ingestion.

Build and push the `dsb-tools` image first. It contains the same pinned upstream revision's `wrk2` binary and `init_social_graph.py`. Supply its fully qualified image reference to installation and session execution. The benchmark template follows the upstream `wrk2` invocation; request rate, connections, duration, endpoint, and resource request are supplied from the completed workload profile, never invented by the manifest.

The seed Job is a real upstream initialization command, but it cannot guarantee a clean state by itself. Before every main trial, the experiment operator must run and record a reset procedure that demonstrably returns the selected regional release to the same fixture state. A practical simple approach is a scoped Helm release reinstall plus the seed Job, after validating the chart's persistence behavior in a smoke run. Do not claim fixture equivalence until the reset output is logged.

`remote-executor-rbac.yaml` is optional. If the primary controller needs to create Jobs in the other cluster, issue it a separate credential limited to this Role. Never mount an Azure administrator kubeconfig in the controller pod.
