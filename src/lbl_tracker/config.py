"""Configuration loading: config.yaml + environment (.env if present)."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml

REPO_ROOT = Path(os.environ.get("LBL_REPO_ROOT", Path(__file__).resolve().parents[2]))


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ and value:
            os.environ[key] = value


@lru_cache(maxsize=1)
def load_config() -> dict:
    _load_dotenv(REPO_ROOT / ".env")
    with open(REPO_ROOT / "config.yaml") as fh:
        return yaml.safe_load(fh)


def cfg(*keys, default=None):
    node = load_config()
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


def data_dir() -> Path:
    return REPO_ROOT / cfg("store", "parquet_dir", default="data/parquet")


def logs_dir() -> Path:
    path = REPO_ROOT / cfg("store", "logs_dir", default="data/logs")
    path.mkdir(parents=True, exist_ok=True)
    return path


def user_agent(sec: bool = False) -> str:
    base = cfg("contact", "user_agent", default="lbl-tracker/0.1")
    if sec:
        # SEC documents exactly "Company Name AdminContact@domain.com";
        # URLs/slashes in the UA still trip their automated-tool filter.
        contact = os.environ.get("SEC_CONTACT_EMAIL", "").strip()
        if contact:
            return f"hdcapital lbl-tracker {contact}"
    return base
