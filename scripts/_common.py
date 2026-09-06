"""Shared command-line bootstrap and small configuration helpers."""

from __future__ import annotations

import sys
import os
from datetime import datetime
from pathlib import Path
from typing import Any


# When running from a checkout, keep repository-relative paths anchored to the
# checkout.  A wheel has no source checkout alongside it, so use the caller's
# working directory (or an explicit operator override) instead.
_SOURCE_ROOT = Path(__file__).resolve().parents[1]
if (_SOURCE_ROOT / "pyproject.toml").exists():
    REPO_ROOT = _SOURCE_ROOT
else:
    REPO_ROOT = Path(os.environ.get("CARBON_SCHEDULER_REPO_ROOT", Path.cwd())).resolve()
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


def load_project_environment(
    path: str | Path | None = None,
    *,
    override: bool = False,
) -> Path | None:
    """Load the repository's ignored ``.env`` file when available.

    Shell-provided values win by default.  This keeps CI/Kubernetes secrets
    authoritative while allowing a local checkout to be started from one
    operator-managed file.  The function deliberately returns only the path;
    it never prints values or copies secrets into run artifacts.
    """

    dotenv_path = resolve_path(path or ".env")
    if not dotenv_path.exists():
        return None
    try:
        from dotenv import load_dotenv
    except ImportError as error:  # pragma: no cover - guarded by package dependency
        raise RuntimeError(
            "python-dotenv is required to load .env; reinstall the project dependencies"
        ) from error
    load_dotenv(dotenv_path=dotenv_path, override=override)
    return dotenv_path


def parse_datetime(value: str) -> datetime:
    """Parse an ISO timestamp and require an explicit timezone."""

    from datetime import timezone

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"invalid ISO timestamp: {value}") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"timestamp must include a timezone: {value}")
    return parsed.astimezone(timezone.utc)


def resolve_path(value: str | Path, *, base: Path = REPO_ROOT) -> Path:
    path = Path(value)
    return path if path.is_absolute() else base / path


# Python entry-point scripts import this module before reading any required
# environment variable.  Loading here makes the documented `.env` workflow
# work for local commands without changing Kubernetes/CI precedence.
load_project_environment()


def required_environment(name: str) -> str:
    import os

    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"required environment variable is not set: {name}")
    return value


def read_json(path: str | Path) -> Any:
    import json

    return json.loads(resolve_path(path).read_text(encoding="utf-8"))


def json_dump(value: Any) -> str:
    import json

    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, default=str)
