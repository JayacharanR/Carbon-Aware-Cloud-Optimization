"""Manifest-derived dependency graph support.

The graph in this module is intentionally conservative: an edge is emitted only
when a manifest explicitly declares ``depends_on`` or contains a reference to a
known service name.  It describes deployment configuration, not observed
runtime traffic.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Iterator


_MANIFEST_SUFFIXES = {".yaml", ".yml", ".json"}
_WORKLOAD_KINDS = {"deployment", "statefulset", "daemonset", "job", "cronjob"}
_NODE_KINDS = _WORKLOAD_KINDS | {"service", "pod", "replicaset"}


@dataclass(slots=True)
class GraphNode:
    """A logical component discovered in one or more manifests."""

    name: str
    kinds: set[str] = field(default_factory=set)
    namespaces: set[str] = field(default_factory=set)
    source_paths: set[str] = field(default_factory=set)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kinds": sorted(self.kinds),
            "namespaces": sorted(self.namespaces),
            "source_paths": sorted(self.source_paths),
        }


@dataclass(frozen=True, slots=True)
class GraphEdge:
    """A declared or manifest-referenced relationship between components."""

    source: str
    target: str
    relationship: str
    source_path: str
    evidence: str

    def to_dict(self) -> dict[str, str]:
        return {
            "source": self.source,
            "target": self.target,
            "relationship": self.relationship,
            "source_path": self.source_path,
            "evidence": self.evidence,
        }


@dataclass(slots=True)
class ManifestDependencyGraph:
    """A serialisable graph generated from deployment manifests."""

    nodes: dict[str, GraphNode] = field(default_factory=dict)
    edges: list[GraphEdge] = field(default_factory=list)
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    source_type: str = "manifest-derived"

    def add_node(
        self,
        name: str,
        *,
        kind: str,
        namespace: str | None,
        source_path: str,
    ) -> None:
        normalized = _normalize_name(name)
        if not normalized:
            return
        node = self.nodes.setdefault(normalized, GraphNode(name=normalized))
        node.kinds.add(kind.lower())
        if namespace:
            node.namespaces.add(namespace)
        node.source_paths.add(source_path)

    def add_edge(
        self,
        source: str,
        target: str,
        *,
        relationship: str,
        source_path: str,
        evidence: str,
    ) -> None:
        source_name = _normalize_name(source)
        target_name = _normalize_name(target)
        if not source_name or not target_name or source_name == target_name:
            return
        candidate = GraphEdge(
            source=source_name,
            target=target_name,
            relationship=relationship,
            source_path=source_path,
            evidence=evidence,
        )
        if candidate not in self.edges:
            self.edges.append(candidate)

    def dependencies_for(self, service: str) -> list[GraphEdge]:
        """Return edges leaving ``service`` without inferring reverse edges."""

        normalized = _normalize_name(service)
        return [edge for edge in self.edges if edge.source == normalized]

    def context_for(self, service: str) -> dict[str, Any]:
        """Produce the compact, traceable context used by the scheduler."""

        normalized = _normalize_name(service)
        node = self.nodes.get(normalized)
        dependencies = self.dependencies_for(normalized)
        return {
            "source_type": self.source_type,
            "service": normalized,
            "service_exists": node is not None,
            "node": node.to_dict() if node else None,
            "dependencies": [edge.to_dict() for edge in dependencies],
            "allowed_services": sorted(self.nodes),
            "graph_hash": self.digest(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type,
            "generated_at": self.generated_at.isoformat(),
            "nodes": [self.nodes[name].to_dict() for name in sorted(self.nodes)],
            "edges": [edge.to_dict() for edge in sorted(self.edges, key=lambda item: (
                item.source,
                item.target,
                item.relationship,
                item.source_path,
                item.evidence,
            ))],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ManifestDependencyGraph":
        generated_at = value.get("generated_at")
        graph = cls(
            generated_at=datetime.fromisoformat(generated_at) if generated_at else datetime.now(UTC),
            source_type=str(value.get("source_type", "manifest-derived")),
        )
        for item in value.get("nodes", []):
            name = str(item["name"])
            graph.nodes[name] = GraphNode(
                name=name,
                kinds=set(item.get("kinds", [])),
                namespaces=set(item.get("namespaces", [])),
                source_paths=set(item.get("source_paths", [])),
            )
        for item in value.get("edges", []):
            graph.edges.append(GraphEdge(**item))
        return graph

    def digest(self) -> str:
        """Return a stable hash of graph content, excluding generation time."""

        canonical = self.to_dict()
        canonical.pop("generated_at", None)
        payload = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def save(self, destination: str | Path) -> Path:
        destination_path = Path(destination)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        destination_path.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
        return destination_path

    @classmethod
    def load(cls, source: str | Path) -> "ManifestDependencyGraph":
        return cls.from_dict(json.loads(Path(source).read_text(encoding="utf-8")))


@dataclass(frozen=True, slots=True)
class _Resource:
    name: str
    kind: str
    namespace: str | None
    source_path: str
    document: dict[str, Any]


def ingest_manifests(manifest_root: str | Path) -> ManifestDependencyGraph:
    """Build a graph from YAML/JSON Kubernetes and Compose manifests.

    Invalid files are skipped only when they are not parseable manifests.  A
    syntactically invalid YAML file raises ``ValueError`` with its file path so
    a deployment mistake is visible rather than silently hidden.
    """

    root = Path(manifest_root)
    if not root.exists():
        raise FileNotFoundError(f"Manifest path does not exist: {root}")

    files = [root] if root.is_file() else sorted(
        path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in _MANIFEST_SUFFIXES
    )
    graph = ManifestDependencyGraph()
    resources: list[_Resource] = []
    compose_docs: list[tuple[str, dict[str, Any]]] = []

    for path in files:
        for document in _load_documents(path):
            if not isinstance(document, dict) or not document:
                continue
            relative_path = str(path.relative_to(root.parent if root.is_file() else root))
            if _is_compose_document(document):
                compose_docs.append((relative_path, document))
                for service_name in document.get("services", {}):
                    graph.add_node(service_name, kind="compose-service", namespace=None, source_path=relative_path)
                continue

            kind = str(document.get("kind", "")).lower()
            metadata = document.get("metadata") or {}
            name = metadata.get("name") if isinstance(metadata, dict) else None
            if kind not in _NODE_KINDS or not isinstance(name, str) or not name.strip():
                continue
            namespace = metadata.get("namespace") if isinstance(metadata, dict) else None
            resource = _Resource(
                name=_normalize_name(name),
                kind=kind,
                namespace=str(namespace) if namespace else None,
                source_path=relative_path,
                document=document,
            )
            resources.append(resource)
            graph.add_node(resource.name, kind=kind, namespace=resource.namespace, source_path=relative_path)

    known_names = set(graph.nodes)
    for relative_path, document in compose_docs:
        for source, service in (document.get("services") or {}).items():
            if not isinstance(service, dict):
                continue
            depends_on = service.get("depends_on", [])
            targets = depends_on.keys() if isinstance(depends_on, dict) else depends_on
            for target in targets if isinstance(targets, Iterable) and not isinstance(targets, str) else []:
                normalized_target = _normalize_name(str(target))
                if normalized_target in known_names:
                    graph.add_edge(
                        source,
                        normalized_target,
                        relationship="declares_dependency",
                        source_path=relative_path,
                        evidence="compose.depends_on",
                    )
            for target, evidence in _known_references(service, known_names):
                graph.add_edge(
                    source,
                    target,
                    relationship="references_service",
                    source_path=relative_path,
                    evidence=evidence,
                )

    for resource in resources:
        for target, evidence in _known_references(resource.document, known_names):
            graph.add_edge(
                resource.name,
                target,
                relationship="references_service",
                source_path=resource.source_path,
                evidence=evidence,
            )
    return graph


def _load_documents(path: Path) -> Iterator[dict[str, Any]]:
    content = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        try:
            loaded = json.loads(content)
        except json.JSONDecodeError as error:
            raise ValueError(f"Invalid JSON manifest: {path}") from error
        if isinstance(loaded, list):
            yield from (item for item in loaded if isinstance(item, dict))
        elif isinstance(loaded, dict):
            yield loaded
        return
    try:
        import yaml
    except ImportError as error:  # pragma: no cover - controlled by project dependencies
        raise RuntimeError("PyYAML is required to ingest YAML manifests") from error
    try:
        yield from (item for item in yaml.safe_load_all(content) if isinstance(item, dict))
    except yaml.YAMLError as error:
        raise ValueError(f"Invalid YAML manifest: {path}") from error


def _is_compose_document(document: dict[str, Any]) -> bool:
    return "services" in document and "kind" not in document and "apiVersion" not in document


def _known_references(document: Any, known_names: set[str]) -> Iterator[tuple[str, str]]:
    """Find known service names in scalar manifest values.

    The matching boundary permits common Kubernetes host forms such as
    ``mongo:27017`` and ``mongo.namespace.svc`` while preventing a service
    named ``api`` from matching unrelated words such as ``capability``.
    """

    for location, value in _walk_scalars(document):
        if not isinstance(value, str):
            continue
        for candidate in known_names:
            expression = rf"(?<![a-z0-9-]){re.escape(candidate)}(?=$|[.:/\\s])"
            if re.search(expression, value.lower()):
                yield candidate, location


def _walk_scalars(value: Any, prefix: str = "$") -> Iterator[tuple[str, str]]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk_scalars(child, f"{prefix}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_scalars(child, f"{prefix}[{index}]")
    elif isinstance(value, str):
        yield prefix, value


def _normalize_name(value: str) -> str:
    return value.strip().lower()


class Neo4jGraphStore:
    """Optional Neo4j adapter for a :class:`ManifestDependencyGraph`.

    The controller can operate locally without Neo4j; importing this class does
    not require the Neo4j driver until a connection is opened.
    """

    def __init__(self, uri: str, username: str, password: str, database: str = "neo4j") -> None:
        self.uri = uri
        self.username = username
        self.password = password
        self.database = database

    def write_graph(self, graph: ManifestDependencyGraph) -> None:
        try:
            from neo4j import GraphDatabase
        except ImportError as error:  # pragma: no cover - optional deployment dependency
            raise RuntimeError("neo4j package is required to write the graph") from error
        driver = GraphDatabase.driver(self.uri, auth=(self.username, self.password))
        try:
            with driver.session(database=self.database) as session:
                for node in graph.nodes.values():
                    session.run(
                        """
                        MERGE (n:ManifestComponent {name: $name})
                        SET n.kinds = $kinds, n.namespaces = $namespaces,
                            n.source_paths = $source_paths, n.source_type = $source_type
                        """,
                        name=node.name,
                        kinds=sorted(node.kinds),
                        namespaces=sorted(node.namespaces),
                        source_paths=sorted(node.source_paths),
                        source_type=graph.source_type,
                    )
                for edge in graph.edges:
                    session.run(
                        """
                        MATCH (source:ManifestComponent {name: $source})
                        MATCH (target:ManifestComponent {name: $target})
                        MERGE (source)-[r:DECLARES_DEPENDENCY {
                            relationship: $relationship,
                            source_path: $source_path,
                            evidence: $evidence
                        }]->(target)
                        """,
                        **edge.to_dict(),
                    )
        finally:
            driver.close()
