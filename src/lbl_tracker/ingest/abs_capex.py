"""ABS 5625.0 Private New Capital Expenditure - mining, actual + expected.

Uses the ABS Data API (SDMX-JSON). The dataflow id is discovered from the
/dataflow endpoint rather than hardcoded blind; the resolved id is cached
here once verified (see SOURCES.md) but a rename falls back to discovery.
"""
from __future__ import annotations

import logging

import pandas as pd

from ..store import log_gap, now_utc, write_observations
from . import abs_sdmx

log = logging.getLogger("lbl_tracker.abs_capex")

SOURCE = "abs_capex"
# Verified against the live /dataflow endpoint (see SOURCES.md): CAPEX is the
# 5625.0 quarterly private new capital expenditure flow.
FLOW_CANDIDATES = ["CAPEX"]
FLOW_KEYWORD = "capital expenditure"
SOURCE_URL = "https://api.data.abs.gov.au/rest/data/ABS,{flow}/all"


def resolve_flow() -> str:
    flows = abs_sdmx.list_dataflows()
    for cand in FLOW_CANDIDATES:
        if (flows["id"] == cand).any():
            return cand
    matches = abs_sdmx.find_dataflows(FLOW_KEYWORD)
    if len(matches):
        picked = matches.iloc[0]["id"]
        log.warning("CAPEX flow candidates missing; discovered %s via keyword", picked)
        return picked
    raise RuntimeError(f"no ABS dataflow matching {FLOW_KEYWORD!r} found")


def _name_col(df: pd.DataFrame, *keywords: str) -> str | None:
    """Find the dimension column whose label-values mention all keywords."""
    for col in df.columns:
        if not col.endswith("_name"):
            continue
        vals = " | ".join(str(v).lower() for v in df[col].dropna().unique())
        if all(k.lower() in vals for k in keywords):
            return col
    return None


def fetch() -> pd.DataFrame:
    flow = resolve_flow()
    df = abs_sdmx.get_data(flow, "all", params={"startPeriod": "1990"})
    if df.empty:
        raise RuntimeError(f"ABS flow {flow} returned no observations")

    industry_col = _name_col(df, "mining")
    if industry_col is None:
        raise RuntimeError(f"no industry dimension with 'Mining' in flow {flow}; "
                           f"columns={list(df.columns)}")
    mining = df[df[industry_col].str.contains("Mining", case=False, na=False)].copy()

    # Total new capital expenditure (not the equipment/buildings split) where
    # such a dimension exists.
    asset_col = _name_col(mining, "total")
    if asset_col and asset_col != industry_col:
        total_mask = mining[asset_col].str.contains("total", case=False, na=False)
        if total_mask.any():
            mining = mining[total_mask]

    retrieved = now_utc()
    url = SOURCE_URL.format(flow=flow)
    out_frames = []

    # Actual vs expected split lives in a "data type" style dimension.
    dtype_col = _name_col(mining, "actual")
    for series_id, keyword in [("abs.capex.mining_actual", "actual"),
                               ("abs.capex.mining_expected", "expect")]:
        if dtype_col:
            sel = mining[mining[dtype_col].str.contains(keyword, case=False, na=False)]
        else:
            sel = mining if keyword == "actual" else mining.iloc[0:0]
        if sel.empty:
            log_gap(SOURCE, series_id, f"no rows matching {keyword!r} in flow {flow} "
                                       f"(dtype_col={dtype_col})")
            continue
        # Prefer seasonally adjusted, then trend, then original.
        adj_col = _name_col(sel, "seasonal")
        if adj_col:
            for pref in ("seasonal", "trend", "original"):
                pick = sel[sel[adj_col].str.contains(pref, case=False, na=False)]
                if len(pick):
                    sel = pick
                    break
        # Collapse any remaining duplicate periods by preferring the first
        # remaining sub-series (deterministic order), never by averaging.
        keep_cols = [c for c in sel.columns if c.endswith("_name")]
        sel = sel.sort_values(keep_cols)
        sel = sel.drop_duplicates("TIME_PERIOD", keep="first")
        out_frames.append(pd.DataFrame({
            "series_id": series_id,
            "date": sel["TIME_PERIOD"].map(abs_sdmx.parse_time_period),
            "value": sel["value"].values,
            "source_url": url,
            "retrieved_at": retrieved,
        }))

    if not out_frames:
        raise RuntimeError(f"ABS CAPEX: nothing matched in flow {flow}")
    return pd.concat(out_frames, ignore_index=True)


def ingest() -> dict:
    return write_observations(SOURCE, fetch())
