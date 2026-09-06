"""Time-bounded retrieval for controller context.

The local store is the default for replay and offline development.  The Qdrant
adapter uses the same record shape and enforces an ``event_time <= as_of``
filter so a decision cannot retrieve later experiment outcomes.
"""

from __future__ import annotations

import hashlib
import math
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable, Iterable, Protocol, Sequence


_TOKEN_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]*", re.IGNORECASE)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("context event_time must include a timezone")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class ContextRecord:
    """A traceable document eligible for scheduling retrieval."""

    record_id: str
    run_id: str
    event_time: datetime
    source_type: str
    region: str | None
    content: str
    content_hash: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        event_time: datetime,
        source_type: str,
        region: str | None,
        content: str,
        metadata: dict[str, Any] | None = None,
        record_id: str | None = None,
    ) -> "ContextRecord":
        normalized_time = _utc(event_time)
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        stable_id = record_id or str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                "|".join((run_id, normalized_time.isoformat(), source_type, region or "", content_hash)),
            )
        )
        return cls(
            record_id=stable_id,
            run_id=run_id,
            event_time=normalized_time,
            source_type=source_type,
            region=region,
            content=content,
            content_hash=content_hash,
            metadata=dict(metadata or {}),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "run_id": self.run_id,
            "event_time": self.event_time.isoformat(),
            # Keep the human-readable timestamp for artifacts, but use a
            # numeric field for Qdrant's range filter.  Qdrant range
            # conditions are reliably supported for numeric payloads across
            # the client/server versions used by this prototype.
            "event_time_epoch": self.event_time.timestamp(),
            "source_type": self.source_type,
            "region": self.region,
            "content": self.content,
            "content_hash": self.content_hash,
            "metadata": self.metadata,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "ContextRecord":
        event_time = datetime.fromisoformat(str(payload["event_time"]).replace("Z", "+00:00"))
        content = str(payload["content"])
        expected_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        supplied_hash = str(payload.get("content_hash", expected_hash))
        if supplied_hash != expected_hash:
            raise ValueError(f"Context record {payload.get('record_id', '<unknown>')} has an invalid content hash")
        return cls(
            record_id=str(payload["record_id"]),
            run_id=str(payload["run_id"]),
            event_time=_utc(event_time),
            source_type=str(payload["source_type"]),
            region=str(payload["region"]) if payload.get("region") is not None else None,
            content=content,
            content_hash=supplied_hash,
            metadata=dict(payload.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class RetrievalQuery:
    query_text: str
    as_of: datetime
    limit: int = 8
    run_id: str | None = None
    region: str | None = None
    source_types: tuple[str, ...] = ()

    def normalized_as_of(self) -> datetime:
        return _utc(self.as_of)


class ContextStore(Protocol):
    def upsert(self, records: Sequence[ContextRecord]) -> None: ...

    def search(self, query: RetrievalQuery) -> list[ContextRecord]: ...


class HashingEmbedder:
    """Small deterministic embedding for local/offline Qdrant development.

    It is a retrieval utility, not an evaluator or a source of experiment
    metrics.  Production deployments may inject a semantic embedder with the
    same callable interface.
    """

    def __init__(self, dimension: int = 128) -> None:
        if dimension < 8:
            raise ValueError("Embedding dimension must be at least 8")
        self.dimension = dimension

    def __call__(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        for token in _tokenize(text):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            value = int.from_bytes(digest, "big")
            index = value % self.dimension
            vector[index] += -1.0 if value & 1 else 1.0
        magnitude = math.sqrt(sum(value * value for value in vector))
        return [value / magnitude for value in vector] if magnitude else vector


class InMemoryContextStore:
    """Lexical, timestamp-filtered context store used by local replay."""

    def __init__(self) -> None:
        self._records: dict[str, ContextRecord] = {}

    def upsert(self, records: Sequence[ContextRecord]) -> None:
        for record in records:
            if record.content_hash != hashlib.sha256(record.content.encode("utf-8")).hexdigest():
                raise ValueError(f"Context record {record.record_id} has an invalid content hash")
            self._records[record.record_id] = record

    def search(self, query: RetrievalQuery) -> list[ContextRecord]:
        if query.limit < 1:
            return []
        query_tokens = set(_tokenize(query.query_text))
        as_of = query.normalized_as_of()
        matches: list[tuple[float, ContextRecord]] = []
        for record in self._records.values():
            if record.event_time > as_of:
                continue
            if query.run_id is not None and record.run_id != query.run_id:
                continue
            if query.region is not None and record.region not in {None, query.region}:
                continue
            if query.source_types and record.source_type not in query.source_types:
                continue
            content_tokens = set(_tokenize(record.content))
            overlap = len(query_tokens & content_tokens)
            # Stable tie-breakers make replay outputs repeatable.
            score = overlap / max(1, len(query_tokens))
            matches.append((score, record))
        matches.sort(key=lambda item: (-item[0], -item[1].event_time.timestamp(), item[1].record_id))
        return [record for _, record in matches[: query.limit]]


class QdrantContextStore:
    """Qdrant-backed context store with mandatory timestamp filtering."""

    def __init__(
        self,
        *,
        url: str,
        collection_name: str = "scheduler_context",
        api_key: str | None = None,
        embedder: Callable[[str], list[float]] | None = None,
        embedding_dimension: int = 128,
    ) -> None:
        self.url = url
        self.collection_name = collection_name
        self.api_key = api_key
        self.embedder = embedder or HashingEmbedder(embedding_dimension)
        self.embedding_dimension = embedding_dimension
        self._client: Any | None = None

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                from qdrant_client import QdrantClient
            except ImportError as error:  # pragma: no cover - deployment optional dependency
                raise RuntimeError("qdrant-client is required for Qdrant retrieval") from error
            self._client = QdrantClient(url=self.url, api_key=self.api_key)
            self._ensure_collection()
        return self._client

    def _ensure_collection(self) -> None:
        try:
            from qdrant_client.models import Distance, VectorParams
        except ImportError as error:  # pragma: no cover
            raise RuntimeError("qdrant-client is required for Qdrant retrieval") from error
        if not self._client.collection_exists(self.collection_name):
            self._client.create_collection(
                collection_name=self.collection_name,
                vectors_config=VectorParams(size=self.embedding_dimension, distance=Distance.COSINE),
            )

    def upsert(self, records: Sequence[ContextRecord]) -> None:
        if not records:
            return
        try:
            from qdrant_client.models import PointStruct
        except ImportError as error:  # pragma: no cover
            raise RuntimeError("qdrant-client is required for Qdrant retrieval") from error
        points = [
            PointStruct(id=record.record_id, vector=self.embedder(record.content), payload=record.to_payload())
            for record in records
        ]
        self.client.upsert(collection_name=self.collection_name, points=points, wait=True)

    def search(self, query: RetrievalQuery) -> list[ContextRecord]:
        if query.limit < 1:
            return []
        try:
            from qdrant_client.models import FieldCondition, Filter, MatchValue, Range
        except ImportError as error:  # pragma: no cover
            raise RuntimeError("qdrant-client is required for Qdrant retrieval") from error
        conditions: list[Any] = [
            FieldCondition(
                key="event_time_epoch",
                range=Range(lte=query.normalized_as_of().timestamp()),
            )
        ]
        if query.run_id is not None:
            conditions.append(FieldCondition(key="run_id", match=MatchValue(value=query.run_id)))
        if query.region is not None:
            conditions.append(FieldCondition(key="region", match=MatchValue(value=query.region)))
        if query.source_types:
            # Qdrant's MatchAny is not present in some old supported clients;
            # run one deterministic query per source type and deduplicate below.
            return self._search_by_source_types(query, conditions)
        return self._query_points(query, Filter(must=conditions))

    def _search_by_source_types(self, query: RetrievalQuery, base_conditions: list[Any]) -> list[ContextRecord]:
        try:
            from qdrant_client.models import FieldCondition, Filter, MatchValue
        except ImportError as error:  # pragma: no cover
            raise RuntimeError("qdrant-client is required for Qdrant retrieval") from error
        records: dict[str, ContextRecord] = {}
        for source_type in sorted(query.source_types):
            conditions = [*base_conditions, FieldCondition(key="source_type", match=MatchValue(value=source_type))]
            for record in self._query_points(query, Filter(must=conditions)):
                records[record.record_id] = record
        return sorted(records.values(), key=lambda record: (-record.event_time.timestamp(), record.record_id))[: query.limit]

    def _query_points(self, query: RetrievalQuery, query_filter: Any) -> list[ContextRecord]:
        vector = self.embedder(query.query_text)
        client = self.client
        if hasattr(client, "query_points"):
            response = client.query_points(
                collection_name=self.collection_name,
                query=vector,
                query_filter=query_filter,
                limit=query.limit,
                with_payload=True,
            )
            points = response.points
        else:  # compatibility with older qdrant-client releases
            points = client.search(
                collection_name=self.collection_name,
                query_vector=vector,
                query_filter=query_filter,
                limit=query.limit,
                with_payload=True,
            )
        records = [ContextRecord.from_payload(dict(point.payload or {})) for point in points]
        # Defend against malformed/old payloads even if the server filter is bypassed.
        as_of = query.normalized_as_of()
        return [record for record in records if record.event_time <= as_of]


class ContextRetriever:
    """High-level retrieval facade used by the workflow context node."""

    def __init__(self, store: ContextStore) -> None:
        self.store = store

    def add_event(
        self,
        *,
        run_id: str,
        event_time: datetime,
        source_type: str,
        content: str,
        region: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ContextRecord:
        record = ContextRecord.create(
            run_id=run_id,
            event_time=event_time,
            source_type=source_type,
            region=region,
            content=content,
            metadata=metadata,
        )
        self.store.upsert([record])
        return record

    def retrieve(
        self,
        *,
        query_text: str,
        as_of: datetime,
        run_id: str | None = None,
        region: str | None = None,
        source_types: Iterable[str] = (),
        limit: int = 8,
    ) -> list[ContextRecord]:
        return self.store.search(
            RetrievalQuery(
                query_text=query_text,
                as_of=as_of,
                run_id=run_id,
                region=region,
                source_types=tuple(source_types),
                limit=limit,
            )
        )


def render_retrieval_context(records: Iterable[ContextRecord]) -> str:
    """Render records deterministically for the LLM prompt and run artifact."""

    ordered = sorted(records, key=lambda record: (record.event_time, record.record_id))
    return "\n\n".join(
        "\n".join(
            (
                f"[evidence_id={record.record_id}]",
                f"source_type={record.source_type}",
                f"event_time={record.event_time.isoformat()}",
                f"region={record.region or ''}",
                record.content,
            )
        )
        for record in ordered
    )


def _tokenize(value: str) -> list[str]:
    return [token.lower() for token in _TOKEN_PATTERN.findall(value)]
