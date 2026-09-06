"""Build the manifest-derived dependency graph artifact."""

from __future__ import annotations

import argparse
import os
import sys

try:
    from _common import resolve_path  # type: ignore
except ImportError:
    from scripts._common import resolve_path  # type: ignore

from carbon_scheduler.graph_ingest import Neo4jGraphStore, ingest_manifests  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Ingest pinned DeathStarBench Kubernetes/Compose manifests"
    )
    parser.add_argument("--manifests", required=True, help="manifest directory or file")
    parser.add_argument("--output", required=True, help="graph JSON destination")
    parser.add_argument("--neo4j-uri", help="optional Neo4j URI for writing the same graph")
    parser.add_argument("--neo4j-database", default="neo4j")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        graph = ingest_manifests(resolve_path(args.manifests))
        destination = graph.save(resolve_path(args.output))
        if args.neo4j_uri:
            username = os.environ.get("NEO4J_USERNAME", "").strip()
            password = os.environ.get("NEO4J_PASSWORD", "")
            if not username or not password:
                raise ValueError(
                    "NEO4J_USERNAME and NEO4J_PASSWORD must be set when --neo4j-uri is supplied"
                )
            Neo4jGraphStore(
                args.neo4j_uri,
                username,
                password,
                database=args.neo4j_database,
            ).write_graph(graph)
            print(f"wrote graph to Neo4j at {args.neo4j_uri} database={args.neo4j_database}")
        print(
            f"wrote graph {destination}: nodes={len(graph.nodes)} edges={len(graph.edges)} "
            f"sha256={graph.digest()}"
        )
        return 0
    except Exception as error:
        print(f"graph ingestion failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
