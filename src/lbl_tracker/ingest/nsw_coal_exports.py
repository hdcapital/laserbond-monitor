"""NSW coal export values - ABS Merchandise Exports (MERCH_EXP), monthly.

SITC division 32 (coal, coke and briquettes), state of origin New South
Wales, all destinations. Values are A$ thousand, original series, monthly
from 1995-07. Verified live 2026-08-21 (see SOURCES.md): key 32.TOT.1.M on
dataflow ABS,MERCH_EXP,1.0.0.

Rationale: export value = volume x price = the cashflow of LaserBond's
NSW mining customer base. Validated against actual LBL segment history
before inclusion - correlates r=+0.87 (p=0.001) with Services revenue
YoY at a 6-month lead on FY21-FY26 halves, r=+0.66 (p=0.026) at a 1-year
lead on the FY16-FY26 annual sample.
"""
from __future__ import annotations

import logging

import pandas as pd

from ..store import now_utc, write_observations
from . import abs_sdmx

log = logging.getLogger("lbl_tracker.nsw_coal_exports")

SOURCE = "nsw_coal_exports"
FLOW = "MERCH_EXP"
# COMMODITY_SITC=32 (coal, coke, briquettes) . COUNTRY_DEST=TOT (all)
# . STATE_ORIGIN=1 (New South Wales) . FREQ=M
KEY = "32.TOT.1.M"
SERIES_ID = "abs.merch_exp.nsw_coal_value"


def fetch() -> pd.DataFrame:
    df = abs_sdmx.get_data(FLOW, KEY, params={"startPeriod": "1990-01"})
    if df.empty:
        raise RuntimeError("ABS MERCH_EXP returned no observations for NSW coal")
    dates = df["TIME_PERIOD"].map(abs_sdmx.parse_time_period)
    values = pd.to_numeric(df["value"], errors="coerce")
    # Sanity: monthly NSW coal export values have ranged ~A$0.15bn-6bn
    # (stored figures are A$ thousand). Anything outside means the unit
    # or key drifted - fail loudly rather than store garbage.
    ok = values.between(5e4, 2e7) | values.isna()
    if not ok.all():
        bad = values[~ok]
        raise RuntimeError("NSW coal export values outside sane range: "
                           f"{bad.min()}..{bad.max()} (A$ thousand expected)")
    out = pd.DataFrame({
        "series_id": SERIES_ID,
        "date": dates,
        "value": values,
        "source_url": f"{abs_sdmx.resolve_base()}/rest/data/ABS,{FLOW},1.0.0/{KEY}",
        "retrieved_at": now_utc(),
    }).sort_values("date")
    log.info("%s: %d rows (%s..%s)", SERIES_ID, len(out),
             out["date"].min().date(), out["date"].max().date())
    return out


def ingest() -> dict:
    return write_observations(SOURCE, fetch())
