"""Where the CLI keeps what it signed in with: one file, readable by its owner alone.

    METAPHI_API_KEY   an organization's key (mai_sk_...): overrides the saved one, for servers and CI
    METAPHI_HOME      the directory the file lives in (default ~/.config/metaphi)
    METAPHI_ACCOUNTS  the accounts host (default https://app.metaphi.ai)
    METAPHI_KENNET    the anonymize endpoint (default https://kennet.metaphi.ai/mcp/)
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

ACCOUNTS = os.environ.get("METAPHI_ACCOUNTS", "https://app.metaphi.ai").rstrip("/")
KENNET = os.environ.get("METAPHI_KENNET", "https://kennet.metaphi.ai/mcp/")
KEY_PREFIX = "mai_sk_"


def home() -> Path:
    return Path(os.environ.get("METAPHI_HOME") or Path.home() / ".config" / "metaphi")


def path() -> Path:
    return home() / "config.json"


def load() -> dict[str, Any]:
    try:
        return json.loads(path().read_text())
    except (OSError, ValueError):
        return {}


def save(cfg: dict[str, Any]) -> None:
    home().mkdir(parents=True, exist_ok=True)
    p = path()
    p.touch(mode=0o600, exist_ok=True)
    os.chmod(p, 0o600)
    p.write_text(json.dumps(cfg, indent=2) + "\n")


def api_key(flag: str | None = None) -> str | None:
    """The key a call runs on: the flag, then METAPHI_API_KEY, then the one saved by `metaphi login --api-key`."""
    return flag or os.environ.get("METAPHI_API_KEY") or load().get("api_key")
