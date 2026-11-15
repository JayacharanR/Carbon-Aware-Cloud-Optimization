from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from carbon_scheduler.carbon_client import CarbonPayloadError, ElectricityMapsClient  # noqa: E402
from carbon_scheduler.schemas import CarbonPoint  # noqa: E402


class _Response:
    status_code = 200

    def json(self) -> dict[str, Any]:
        return {"carbonIntensity": 380, "datetime": "2026-10-09T17:00:00.000Z"}


class _RecordingSession:
    def __init__(self) -> None:
        self.params: list[dict[str, Any]] = []

    def get(self, url: str, *, headers: dict[str, str], params: dict[str, Any], timeout: float) -> _Response:
        self.params.append(params)
        return _Response()


class ElectricityMapsClientTests(unittest.TestCase):
    def _client(self, provider: str, session: _RecordingSession, directory: str) -> ElectricityMapsClient:
        return ElectricityMapsClient(
            api_token="token",
            cache_directory=directory,
            cache_max_age=timedelta(hours=1),
            provider=provider,
            session=session,  # type: ignore[arg-type]
        )

    def test_zone_provider_queries_the_zone_directly(self) -> None:
        session = _RecordingSession()
        with tempfile.TemporaryDirectory() as directory:
            self._client("zone", session, directory)._request("latest", "US-MIDA-PJM")
        self.assertEqual(session.params[0]["zone"], "US-MIDA-PJM")
        self.assertNotIn("dataCenterProvider", session.params[0])

    def test_azure_provider_keeps_data_center_lookup(self) -> None:
        session = _RecordingSession()
        with tempfile.TemporaryDirectory() as directory:
            self._client("azure", session, directory)._request("latest", "eastus2")
        self.assertEqual(session.params[0]["dataCenterProvider"], "azure")
        self.assertEqual(session.params[0]["dataCenterRegion"], "eastus2")
        self.assertNotIn("zone", session.params[0])

    def test_non_finite_intensity_is_not_a_carbon_point(self) -> None:
        """Python's JSON decoder accepts ``Infinity``; the optimiser must never see it."""

        payload = json.loads('{"zone": "US-MIDA-PJM", "carbonIntensity": Infinity, "datetime": "2026-10-28T10:00:00Z"}')
        with self.assertRaises(CarbonPayloadError):
            ElectricityMapsClient._extract_current(payload)
        with self.assertRaises(ValueError):
            CarbonPoint(timestamp=datetime(2026, 10, 28, tzinfo=timezone.utc), carbon_intensity_gco2eq_per_kwh=float("inf"))


if __name__ == "__main__":
    unittest.main()
