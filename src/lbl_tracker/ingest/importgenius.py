"""Optional bill-of-lading shipment data (ImportGenius) - dormant stub.

Stays inert until IMPORTGENIUS_API_KEY is provided; it never writes
placeholder data. When a key is added, implement the account's actual API
plan here (endpoints differ by subscription tier) and wire the series into
config.yaml before use.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("lbl_tracker.importgenius")

SOURCE = "importgenius"


def ingest() -> dict:
    key = os.environ.get("IMPORTGENIUS_API_KEY", "").strip()
    if not key:
        log.info("importgenius: no IMPORTGENIUS_API_KEY - module dormant, nothing written")
        return {"rows_written": 0, "dormant": True}
    raise NotImplementedError(
        "IMPORTGENIUS_API_KEY is set but the account-specific API integration "
        "has not been implemented yet; see module docstring.")
