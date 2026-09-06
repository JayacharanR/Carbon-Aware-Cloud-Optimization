"""Electricity Maps carbon-intensity client with an auditable local cache."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import uuid4

import requests

from .schemas import (
    CarbonApiRecord,
    CarbonDataSource,
    CarbonPoint,
    CarbonRegionData,
    CarbonSnapshot,
    SnapshotMode,
)


class CarbonClientError(RuntimeError):
    """Base error for an unsuccessful carbon-data operation."""


class CarbonPayloadError(CarbonClientError):
    """Raised when a provider response cannot be represented faithfully."""


class CarbonDataUnavailable(CarbonClientError):
    """Raised when neither a live response nor a valid cache snapshot exists."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime:
    value = value or _utc_now()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps supplied to the carbon client must include a timezone")
    return value.astimezone(timezone.utc)


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise CarbonPayloadError("carbon point is missing an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise CarbonPayloadError(f"invalid carbon point timestamp: {value}") from error
    return _as_utc(parsed)


def _safe_region_path(provider_region: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", provider_region.strip().lower())
    if not safe or safe in {".", ".."}:
        raise ValueError("provider region must contain a usable path component")
    return safe


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _content_hash(value: Any) -> str:
    return sha256(_canonical_bytes(value)).hexdigest()


class CarbonCache:
    """Store immutable successful live snapshots plus a mutable latest pointer."""

    def __init__(self, directory: str | Path, max_age: timedelta) -> None:
        self.directory = Path(directory)
        self.max_age = max_age

    def save_live(
        self,
        region_data: CarbonRegionData,
        records: Sequence[CarbonApiRecord],
    ) -> tuple[CarbonRegionData, str]:
        if region_data.source is not CarbonDataSource.LIVE:
            raise ValueError("only successful live responses may be added to the live cache")
        region_directory = self.directory / _safe_region_path(region_data.provider_region)
        region_directory.mkdir(parents=True, exist_ok=True)
        captured = region_data.captured_at.astimezone(timezone.utc)
        filename = f"{captured.strftime('%Y%m%dT%H%M%S%fZ')}-{uuid4().hex}.json"
        snapshot_path = region_directory / filename
        record = {
            "format_version": 1,
            "region_data": region_data.model_dump(mode="json"),
            "api_records": [item.model_dump(mode="json") for item in records],
        }
        record_hash = _content_hash(record)
        record["content_hash"] = record_hash
        self._atomic_write_json(snapshot_path, record)
        self._atomic_write_json(
            region_directory / "latest.json",
            {
                "format_version": 1,
                "snapshot_file": filename,
                "updated_at": captured.isoformat(),
                "content_hash": record_hash,
            },
        )
        stored_region_data = region_data.model_copy(
            update={
                "raw_source_path": str(snapshot_path),
                "raw_source_hash": record_hash,
            }
        )
        return stored_region_data, record_hash

    def load_latest(
        self,
        provider_region: str,
        *,
        now: datetime | None = None,
        fallback_reason: str,
    ) -> CarbonRegionData:
        now = _as_utc(now)
        region_directory = self.directory / _safe_region_path(provider_region)
        pointer_path = region_directory / "latest.json"
        try:
            pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
            snapshot_name = pointer["snapshot_file"]
            if (
                not isinstance(snapshot_name, str)
                or Path(snapshot_name).name != snapshot_name
                or snapshot_name in {"", ".", ".."}
            ):
                raise ValueError("cache pointer contains an invalid snapshot filename")
            snapshot_path = region_directory / snapshot_name
            record = json.loads(snapshot_path.read_text(encoding="utf-8"))
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise CarbonDataUnavailable(
                f"no readable cache snapshot for provider region {provider_region}"
            ) from error
        expected_hash = record.get("content_hash")
        material = {key: value for key, value in record.items() if key != "content_hash"}
        if not isinstance(expected_hash, str) or _content_hash(material) != expected_hash:
            raise CarbonDataUnavailable(
                f"cache snapshot integrity check failed for provider region {provider_region}"
            )
        pointer_hash = pointer.get("content_hash")
        if pointer_hash is not None and pointer_hash != expected_hash:
            raise CarbonDataUnavailable(
                f"cache pointer does not match snapshot for provider region {provider_region}"
            )
        try:
            cached = CarbonRegionData.model_validate(record["region_data"])
        except Exception as error:
            raise CarbonDataUnavailable(
                f"cache snapshot cannot be decoded for provider region {provider_region}"
            ) from error
        if cached.provider_region != provider_region:
            raise CarbonDataUnavailable(
                f"cache snapshot region mismatch for provider region {provider_region}"
            )
        age = now - cached.captured_at.astimezone(timezone.utc)
        if age < timedelta(0):
            raise CarbonDataUnavailable(
                f"cache snapshot timestamp is in the future for provider region {provider_region}"
            )
        if age > self.max_age:
            raise CarbonDataUnavailable(
                f"cache snapshot for provider region {provider_region} exceeds maximum age"
            )
        return cached.model_copy(
            update={
                "source": CarbonDataSource.CACHE,
                "raw_source_path": str(snapshot_path),
                "raw_source_hash": expected_hash,
                "fallback_reason": fallback_reason,
            }
        )

    @staticmethod
    def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
        temporary_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary_path.write_bytes(_canonical_bytes(value))
            os.replace(temporary_path, path)
        finally:
            if temporary_path.exists():
                temporary_path.unlink(missing_ok=True)


class ElectricityMapsClient:
    """Retrieve provider-region carbon intensity and preserve raw source records."""

    def __init__(
        self,
        *,
        api_token: str,
        cache_directory: str | Path,
        cache_max_age: timedelta,
        api_base_url: str = "https://api.electricitymaps.com/v4",
        provider: str = "azure",
        forecast_horizon_hours: int = 24,
        temporal_granularity: str = "5_minutes",
        timeout_seconds: float = 15,
        session: requests.Session | None = None,
    ) -> None:
        if not api_token.strip():
            raise ValueError("Electricity Maps API token must not be empty")
        if forecast_horizon_hours <= 0:
            raise ValueError("forecast_horizon_hours must be positive")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.api_token = api_token
        self.api_base_url = api_base_url.rstrip("/")
        self.provider = provider.lower()
        self.forecast_horizon_hours = forecast_horizon_hours
        self.temporal_granularity = temporal_granularity
        self.timeout_seconds = timeout_seconds
        self.session = session or requests.Session()
        self.cache = CarbonCache(cache_directory, cache_max_age)

    def fetch_region(
        self,
        provider_region: str,
        *,
        now: datetime | None = None,
    ) -> CarbonRegionData:
        """Fetch current and forecast signals, using the newest valid cache on failure."""

        now = _as_utc(now)
        try:
            latest_record = self._request("latest", provider_region)
            forecast_record = self._request("forecast", provider_region)
            current = self._extract_current(latest_record.payload)
            forecast = self._extract_forecast(forecast_record.payload)
            region_data = CarbonRegionData(
                provider_region=provider_region,
                source=CarbonDataSource.LIVE,
                current=current,
                forecast=forecast,
                captured_at=now,
            )
            stored, _ = self.cache.save_live(region_data, [latest_record, forecast_record])
            return stored
        except CarbonClientError as error:
            return self.cache.load_latest(
                provider_region,
                now=now,
                fallback_reason=str(error),
            )

    def fetch_snapshot(
        self,
        regions: Mapping[str, str],
        *,
        decision_time: datetime | None = None,
    ) -> CarbonSnapshot:
        """Retrieve one source record per application region for a decision cycle."""

        decision_time = _as_utc(decision_time)
        if not regions:
            raise ValueError("at least one application region is required")
        region_data = {
            application_region: self.fetch_region(provider_region, now=decision_time)
            for application_region, provider_region in regions.items()
        }
        material = {
            application_region: item.model_dump(mode="json")
            for application_region, item in region_data.items()
        }
        return CarbonSnapshot(
            snapshot_id=str(uuid4()),
            mode=SnapshotMode.CAPTURE,
            decision_time=decision_time,
            regions=region_data,
            source_hash=_content_hash(material),
        )

    def fetch_historical(
        self,
        provider_region: str,
        *,
        start: datetime,
        end: datetime,
    ) -> CarbonApiRecord:
        """Fetch a source response for setup or analysis without synthesising values."""

        start = _as_utc(start)
        end = _as_utc(end)
        if end <= start:
            raise ValueError("historical end must be after start")
        return self._request(
            "historical",
            provider_region,
            extra_params={"start": start.isoformat(), "end": end.isoformat()},
        )

    def _request(
        self,
        query_mode: str,
        provider_region: str,
        *,
        extra_params: Mapping[str, Any] | None = None,
    ) -> CarbonApiRecord:
        requested_at = _utc_now()
        endpoint_by_mode = {
            "latest": "carbon-intensity/latest",
            "forecast": "carbon-intensity/forecast",
            "historical": "carbon-intensity/past-range",
        }
        try:
            endpoint = endpoint_by_mode[query_mode]
        except KeyError as error:
            raise ValueError(f"unsupported query mode: {query_mode}") from error
        params: dict[str, Any] = {
            "dataCenterProvider": self.provider,
            "dataCenterRegion": provider_region,
            "temporalGranularity": self.temporal_granularity,
        }
        if query_mode == "forecast":
            params["horizonHours"] = self.forecast_horizon_hours
        if extra_params:
            params.update(extra_params)
        try:
            response = self.session.get(
                f"{self.api_base_url}/{endpoint}",
                headers={"auth-token": self.api_token},
                params=params,
                timeout=self.timeout_seconds,
            )
            received_at = _utc_now()
        except requests.RequestException as error:
            raise CarbonClientError(
                f"{query_mode} request failed for {provider_region}: {error}"
            ) from error
        status = getattr(response, "status_code", None)
        if not isinstance(status, int):
            raise CarbonClientError(
                f"{query_mode} response for {provider_region} has no HTTP status"
            )
        if status < 200 or status >= 300:
            raise CarbonClientError(
                f"{query_mode} request failed for {provider_region} with HTTP {status}"
            )
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as error:
            raise CarbonClientError(
                f"{query_mode} response for {provider_region} is not JSON"
            ) from error
        if not isinstance(payload, (dict, list)):
            raise CarbonClientError(
                f"{query_mode} response for {provider_region} is not a JSON object or list"
            )
        return CarbonApiRecord(
            query_mode=query_mode,
            provider_region=provider_region,
            requested_at=requested_at,
            received_at=received_at,
            http_status=status,
            payload=payload,
        )

    @staticmethod
    def _extract_current(payload: dict[str, Any] | list[Any] | None) -> CarbonPoint:
        points = ElectricityMapsClient._extract_points(payload, preferred_keys=("data",))
        if not points:
            raise CarbonPayloadError("latest response contains no carbon-intensity point")
        return max(points, key=lambda point: point.timestamp)

    @staticmethod
    def _extract_forecast(payload: dict[str, Any] | list[Any] | None) -> list[CarbonPoint]:
        points = ElectricityMapsClient._extract_points(payload, preferred_keys=("forecast", "data"))
        if not points:
            raise CarbonPayloadError("forecast response contains no carbon-intensity points")
        return sorted(points, key=lambda point: point.timestamp)

    @staticmethod
    def _extract_points(
        payload: dict[str, Any] | list[Any] | None,
        *,
        preferred_keys: tuple[str, ...],
    ) -> list[CarbonPoint]:
        if isinstance(payload, list):
            records: list[Any] = payload
        elif isinstance(payload, dict):
            records = []
            for key in preferred_keys:
                candidate = payload.get(key)
                if isinstance(candidate, list):
                    records = candidate
                    break
            if not records:
                records = [payload]
        else:
            return []

        points: list[CarbonPoint] = []
        for record in records:
            if not isinstance(record, dict):
                continue
            intensity = record.get("carbonIntensity", record.get("value"))
            timestamp = record.get("datetime")
            if intensity is None or timestamp is None:
                continue
            if isinstance(intensity, bool):
                continue
            try:
                numeric_intensity = float(intensity)
                point = CarbonPoint(
                    timestamp=_parse_timestamp(timestamp),
                    carbon_intensity_gco2eq_per_kwh=numeric_intensity,
                )
            except (TypeError, ValueError, CarbonPayloadError):
                continue
            points.append(point)
        return points


def load_replay_trace(path: str | Path) -> list[CarbonSnapshot]:
    """Load frozen JSON or JSONL snapshots and mark their sources as replayed."""

    trace_path = Path(path)
    try:
        text = trace_path.read_text(encoding="utf-8")
    except OSError as error:
        raise CarbonDataUnavailable(f"cannot read replay trace: {trace_path}") from error
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        try:
            decoded = [json.loads(line) for line in text.splitlines() if line.strip()]
        except json.JSONDecodeError as error:
            raise CarbonDataUnavailable(f"replay trace is not JSON or JSONL: {trace_path}") from error
    # Capture CLI writes a provenance envelope around the actual snapshot list;
    # accept that shape as well as the compact JSON/JSONL forms.
    if isinstance(decoded, dict) and isinstance(decoded.get("snapshots"), list):
        raw_snapshots = decoded["snapshots"]
    else:
        raw_snapshots = decoded if isinstance(decoded, list) else [decoded]
    if not raw_snapshots:
        raise CarbonDataUnavailable("replay trace contains no snapshots")

    snapshots: list[CarbonSnapshot] = []
    for raw_snapshot in raw_snapshots:
        try:
            snapshot = CarbonSnapshot.model_validate(raw_snapshot)
        except Exception as error:
            raise CarbonDataUnavailable("replay trace contains an invalid carbon snapshot") from error
        replay_regions = {
            name: item.model_copy(update={"source": CarbonDataSource.REPLAY})
            for name, item in snapshot.regions.items()
        }
        snapshots.append(
            snapshot.model_copy(update={"mode": SnapshotMode.REPLAY, "regions": replay_regions})
        )
    return snapshots
