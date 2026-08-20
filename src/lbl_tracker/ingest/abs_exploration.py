"""ABS 8412.0 Mineral Exploration - metres drilled by state (SDMX API)."""
from __future__ import annotations

import logging

import pandas as pd

from ..store import now_utc, write_observations
from . import abs_sdmx

log = logging.getLogger("lbl_tracker.abs_exploration")

SOURCE = "abs_exploration"
FLOW_CANDIDATES = ["MERALS_EXP", "MIN_EXP", "MINERAL_EXPLORATION"]
FLOW_KEYWORD = "mineral exploration"
SOURCE_URL = "https://api.data.abs.gov.au/rest/data/ABS,{flow}/all"

STATE_SLUGS = {
    "New South Wales": "nsw", "Victoria": "vic", "Queensland": "qld",
    "South Australia": "sa", "Western Australia": "wa", "Tasmania": "tas",
    "Northern Territory": "nt", "Australian Capital Territory": "act",
    "Australia": "total",
}


def resolve_flow() -> str:
    flows = abs_sdmx.list_dataflows()
    for cand in FLOW_CANDIDATES:
        if (flows["id"] == cand).any():
            return cand
    matches = abs_sdmx.find_dataflows(FLOW_KEYWORD)
    if len(matches):
        return matches.iloc[0]["id"]
    raise RuntimeError(f"no ABS dataflow matching {FLOW_KEYWORD!r} found")


def fetch() -> pd.DataFrame:
    flow = resolve_flow()
    df = abs_sdmx.get_data(flow, "all", params={"startPeriod": "1990"})
    if df.empty:
        raise RuntimeError(f"ABS flow {flow} returned no observations")

    # Keep metres-drilled measures only (8412.0 also carries expenditure).
    measure_col = None
    for col in df.columns:
        if col.endswith("_name"):
            vals = " | ".join(str(v).lower() for v in df[col].dropna().unique())
            if "metres" in vals or "drilled" in vals:
                measure_col = col
                break
    if measure_col:
        df = df[df[measure_col].str.contains("metre|drill", case=False, na=False)]
    if df.empty:
        raise RuntimeError(f"ABS {flow}: no metres-drilled rows found")

    # Region dimension.
    region_col = None
    for col in df.columns:
        if col.endswith("_name"):
            vals = set(str(v) for v in df[col].dropna().unique())
            if "Western Australia" in vals or "Queensland" in vals:
                region_col = col
                break
    if region_col is None:
        raise RuntimeError(f"ABS {flow}: no state dimension; cols={list(df.columns)}")

    # Prefer total-type drilling (all deposits, new + existing) and
    # seasonally adjusted where offered; keep deterministic first sub-series
    # per (region, period) otherwise.
    retrieved = now_utc()
    url = SOURCE_URL.format(flow=flow)
    rows = []
    for region_name, slug in STATE_SLUGS.items():
        sel = df[df[region_col] == region_name]
        if sel.empty:
            continue
        adj_col = None
        for col in sel.columns:
            if col.endswith("_name"):
                vals = " | ".join(str(v).lower() for v in sel[col].dropna().unique())
                if "seasonal" in vals or "original" in vals:
                    adj_col = col
                    break
        if adj_col:
            for pref in ("seasonal", "original", "trend"):
                pick = sel[sel[adj_col].str.contains(pref, case=False, na=False)]
                if len(pick):
                    sel = pick
                    break
        name_cols = [c for c in sel.columns if c.endswith("_name")]
        sel = sel.sort_values(name_cols).drop_duplicates("TIME_PERIOD", keep="first")
        series_id = ("abs.exploration.metres_drilled_total" if slug == "total"
                     else f"abs.exploration.metres_drilled_{slug}")
        rows.append(pd.DataFrame({
            "series_id": series_id,
            "date": sel["TIME_PERIOD"].map(abs_sdmx.parse_time_period),
            "value": sel["value"].values,
            "source_url": url,
            "retrieved_at": retrieved,
        }))
    if not rows:
        raise RuntimeError(f"ABS {flow}: no state series extracted")
    return pd.concat(rows, ignore_index=True)


def ingest() -> dict:
    return write_observations(SOURCE, fetch())
