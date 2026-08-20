"""Ingester registry. Each module exposes SOURCE (str) and ingest() -> dict."""
from __future__ import annotations

import importlib
import logging
import traceback

from ..store import log_gap, log_run

log = logging.getLogger("lbl_tracker.ingest")

INGESTERS = [
    "qld_coal",
    "pilbara_ports",
    "abs_capex",
    "abs_exploration",
    "nsw_coal_exports",
    "rba",
    "aisi",
    "fred_steel",
    "baker_hughes",
    "cat_edgar",
    "jsa_ivi",
    "tungsten_news",
    "asx_announcements",
    "importgenius",
]


def get_module(name: str):
    if name not in INGESTERS:
        raise KeyError(f"unknown ingester {name!r}; known: {INGESTERS}")
    return importlib.import_module(f"lbl_tracker.ingest.{name}")


def run(name: str) -> dict:
    mod = get_module(name)
    try:
        stats = mod.ingest()
        log_run(name, "ok", rows=stats.get("rows_written", 0))
        return {"source": name, "status": "ok", **stats}
    except Exception as exc:  # noqa: BLE001 - a failed source must never kill the batch
        log_run(name, "error", error=f"{exc}\n{traceback.format_exc()}")
        log_gap(name, "*", f"ingest failed: {exc}")
        log.error("ingest %s FAILED: %s", name, exc)
        return {"source": name, "status": "error", "error": str(exc)}


def run_all(names: list[str] | None = None) -> list[dict]:
    return [run(name) for name in (names or INGESTERS)]
