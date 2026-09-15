"""Safe runtime release metadata for diagnostics and health responses."""
from __future__ import annotations

import os
import re
from typing import Any


_HEX_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")


def _first_non_empty(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return "unknown"


def _short_commit(commit: str) -> str:
    if _HEX_COMMIT_RE.fullmatch(commit):
        return commit[:7]
    return "unknown"


def get_release_info() -> dict[str, str]:
    """Return non-sensitive release values supplied by the hosting runtime."""
    branch = _first_non_empty("RENDER_GIT_BRANCH", "GIT_BRANCH")
    commit = _first_non_empty("RENDER_GIT_COMMIT", "GIT_COMMIT")
    environment = _first_non_empty("APP_ENV", "ENVIRONMENT")
    if environment == "unknown" and (
        os.getenv("RENDER", "").strip().lower() == "true"
        or os.getenv("RENDER_SERVICE_ID", "").strip()
    ):
        environment = "production"
    return {
        "branch": branch,
        "commit": commit,
        "commit_short": _short_commit(commit),
        "environment": environment,
    }


def release_log_fields() -> dict[str, Any]:
    """Return fields suitable for structured logging without secrets."""
    info = get_release_info()
    return {
        "branch": info["branch"],
        "commit": info["commit"],
        "environment": info["environment"],
    }
