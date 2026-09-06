from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from scripts import bootstrap  # noqa: E402


class BootstrapTests(unittest.TestCase):
    def test_placeholder_detection_and_bicep_escaping(self) -> None:
        self.assertFalse(bootstrap._is_present("REPLACE_ME"))
        self.assertFalse(bootstrap._is_present("<required>"))
        self.assertTrue(bootstrap._is_present("actual-value"))
        self.assertEqual(bootstrap._bicep_string("owner's key"), "'owner''s key'")

    def test_csv_numeric_and_region_override_helpers(self) -> None:
        values = {
            "REGIONS": " eastus, westus2 ",
            "INTEGER": "5",
            "NUMBER": "0.25",
            "TARGET_PRIMARY_IMAGE": "primary-image",
            "COMMON_IMAGE": "common-image",
        }
        self.assertEqual(bootstrap._csv(values, "REGIONS"), ["eastus", "westus2"])
        self.assertEqual(bootstrap._integer(values, "INTEGER"), 5)
        self.assertEqual(bootstrap._number(values, "NUMBER"), 0.25)
        self.assertEqual(
            bootstrap._region_value(values, "PRIMARY", "IMAGE", "COMMON_IMAGE"),
            "primary-image",
        )
        self.assertEqual(
            bootstrap._region_value({"COMMON_IMAGE": "common-image"}, "SECONDARY", "IMAGE", "COMMON_IMAGE"),
            "common-image",
        )

    def test_public_key_loader_reads_only_the_public_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / "id_ed25519.pub"
            key_path.write_text("ssh-ed25519 AAAA operator", encoding="utf-8")
            self.assertEqual(
                bootstrap._ssh_public_key({"AZURE_SSH_PUBLIC_KEY_PATH": str(key_path)}),
                "ssh-ed25519 AAAA operator",
            )

    def test_infrastructure_validation_does_not_require_runtime_services(self) -> None:
        values = {
            "AZURE_SUBSCRIPTION_ID": "subscription",
            "AZURE_RESOURCE_GROUP": "resource-group",
            "AZURE_PRIMARY_REGION": "eastus",
            "AZURE_SECONDARY_REGION": "westus2",
            "AZURE_PRIMARY_CLUSTER": "primary",
            "AZURE_SECONDARY_CLUSTER": "secondary",
            "AZURE_PRIMARY_DNS_PREFIX": "primary-dns",
            "AZURE_SECONDARY_DNS_PREFIX": "secondary-dns",
            "AZURE_DEPLOYMENT_LOCATION": "eastus",
            "AZURE_KUBERNETES_VERSION": "1.29.0",
            "AZURE_BUDGET_NAME": "budget",
            "AZURE_BUDGET_MONTHLY_AMOUNT": "100",
            "AZURE_BUDGET_CONTACT_EMAILS": "owner@example.edu",
            "AZURE_BUDGET_START_DATE": "2026-09-01",
            "AZURE_BUDGET_END_DATE": "2026-10-01",
            "AZURE_SSH_PUBLIC_KEY": "ssh-ed25519 AAAA test",
        }
        self.assertEqual(bootstrap._infra_missing(values), [])

    def test_experiment_validation_requires_pilot_latency_values(self) -> None:
        missing = bootstrap._experiment_missing({})
        self.assertIn("LATENCY_PRIMARY_P95_MS", missing)
        self.assertIn("LATENCY_SECONDARY_P95_MS", missing)

    def test_experiment_renderer_writes_valid_private_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            latency_path = root / "latency_catalog.yaml"
            trace_path = root / "carbon-trace.json"
            values = {
                "AZURE_RESOURCE_GROUP": "resource-group",
                "AZURE_PRIMARY_REGION": "eastus",
                "AZURE_SECONDARY_REGION": "westus2",
                "AZURE_PRIMARY_CLUSTER": "primary",
                "AZURE_SECONDARY_CLUSTER": "secondary",
                "CARBON_PRIMARY_PROVIDER_REGION": "US-EA",
                "CARBON_SECONDARY_PROVIDER_REGION": "US-WA",
                "CARBON_REPLAY_TRACE_PATH": str(trace_path),
                "WORKLOAD_FRONTEND_SERVICE": "nginx-thrift",
                "WORKLOAD_BENCHMARK_SCRIPT": "compose-post.lua",
                "WORKLOAD_PROFILE_ID": "pilot",
                "WORKLOAD_EARLIEST_START": "2026-09-06T09:00:00Z",
                "WORKLOAD_DEADLINE": "2026-09-06T09:30:00Z",
                "SLO_PROFILE_P95_LIMIT_MS": "500",
                "SLO_PROFILE_ERROR_RATE_LIMIT": "0.05",
                "TRUST_THRESHOLD": "0.6",
                "TRUST_WEIGHT_RAGAS_FAITHFULNESS": "0.2",
                "TRUST_WEIGHT_RAGAS_CONTEXT_PRECISION": "0.2",
                "TRUST_WEIGHT_NEMO_RAILS": "0.2",
                "TRUST_WEIGHT_DATA_FRESHNESS": "0.2",
                "TRUST_WEIGHT_EXECUTION_FEASIBILITY": "0.2",
                "MILP_LATENCY_CATALOG_PATH": str(latency_path),
                "EXPERIMENT_STATIC_REFERENCE_REGION": "eastus",
                "EXPERIMENT_RANDOM_SEED": "1",
                "DEATHSTARBENCH_SOURCE": "https://github.com/delimitrou/DeathStarBench.git",
                "DEATHSTARBENCH_COMMIT": "0123456789abcdef0123456789abcdef01234567",
                "DSB_TOOLS_IMAGE": "ghcr.io/example/dsb-tools@sha256:" + "a" * 64,
                "DSB_GRAPH_DATASET": "/opt/deathstarbench/socialNetwork/dataset/graph",
                "DSB_ACTIVE_DEADLINE_SECONDS": "900",
                "DSB_WRK_THREADS": "2",
                "DSB_WRK_CONNECTIONS": "10",
                "DSB_WRK_DURATION": "60s",
                "DSB_REQUESTS_PER_SECOND": "10",
                "DSB_LOADGEN_CPU_REQUEST": "250m",
                "DSB_LOADGEN_MEMORY_REQUEST": "256Mi",
                "TARGET_PRIMARY_KUBE_CONTEXT": "primary-context",
                "TARGET_PRIMARY_FRONTEND_SERVICE": "nginx-thrift",
                "TARGET_PRIMARY_FRONTEND_HOST": "social-network.social-network.svc.cluster.local",
                "TARGET_PRIMARY_FRONTEND_PORT": "8080",
                "TARGET_PRIMARY_TARGET_URL": "http://social-network.social-network.svc.cluster.local:8080",
                "TARGET_SECONDARY_KUBE_CONTEXT": "secondary-context",
                "TARGET_SECONDARY_FRONTEND_SERVICE": "nginx-thrift",
                "TARGET_SECONDARY_FRONTEND_HOST": "social-network.social-network.svc.cluster.local",
                "TARGET_SECONDARY_FRONTEND_PORT": "8080",
                "TARGET_SECONDARY_TARGET_URL": "http://social-network.social-network.svc.cluster.local:8080",
                "LATENCY_PRIMARY_P95_MS": "120",
                "LATENCY_SECONDARY_P95_MS": "140",
                "OLLAMA_BASE_URL": "http://127.0.0.1:11434",
                "OLLAMA_MODEL": "llama3:8b",
            }
            old_root = bootstrap.REPO_ROOT
            bootstrap.REPO_ROOT = root
            try:
                bootstrap._render_experiment(values, force=False)
            finally:
                bootstrap.REPO_ROOT = old_root

            config_path = root / "config" / "experiment.yaml"
            targets_path = root / "config" / "targets.yaml"
            self.assertTrue(config_path.exists())
            self.assertTrue(targets_path.exists())
            self.assertTrue(latency_path.exists())
            loaded = bootstrap.load_experiment_config(config_path)
            self.assertEqual(loaded.azure.primary_region, "eastus")
            self.assertEqual(loaded.workload.profile_id, "pilot")

            raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            raw["trust"]["weights"]["data_freshness"] = 0.1
            config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
            with self.assertRaises(bootstrap.ConfigurationError):
                bootstrap.load_experiment_config(config_path)


if __name__ == "__main__":
    unittest.main()
