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
SOURCE_URL = "https://data.api.abs.gov.au/rest/data/ABS,{flow}/all"


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


# Dimension values confirmed against the live CAPEX flow (2026-08-20):
# MEASURE_name: Actual / Long Term Expected / Short Term Expected Expenditure
# PRICE_ADJUSTMENT_name: Chain Volume Measures / Current Price
# ASSET_name: Buildings and Structures / Equipment... / Total
# INDUSTRY_name includes exactly 'Mining' (plus sub-industries)
# TSEST_name: Original / Seasonally Adjusted / Trend
# STATE_name includes exactly 'Australia'
def _prefer(df: pd.DataFrame, col: str, order: list[str]) -> pd.DataFrame:
    if col not in df.columns:
        return df
    for want in order:
        pick = df[df[col] == want]
        if len(pick):
            return pick
    return df


def fetch() -> pd.DataFrame:
    flow = resolve_flow()
    df = abs_sdmx.get_data(flow, "all", params={"startPeriod": "1987"})
    if df.empty:
        raise RuntimeError(f"ABS flow {flow} returned no observations")

    base = df
    for col, value in [("INDUSTRY_name", "Mining"), ("ASSET_name", "Total"),
                       ("STATE_name", "Australia")]:
        if col in base.columns:
            picked = base[base[col] == value]
            if picked.empty:
                raise RuntimeError(f"ABS {flow}: no rows with {col} == {value!r}; "
                                   f"have {sorted(base[col].dropna().unique())[:20]}")
            base = picked

    retrieved = now_utc()
    url = SOURCE_URL.format(flow=flow)
    out_frames = []
    for series_id, measures in [
        ("abs.capex.mining_actual", ["Actual Expenditure"]),
        ("abs.capex.mining_expected", ["Short Term Expected Expenditure",
                                       "Long Term Expected Expenditure"]),
    ]:
        mcol = "MEASURE_name" if "MEASURE_name" in base.columns else _name_col(base, "actual")
        sel = base[base[mcol].isin(measures)] if mcol else base.iloc[0:0]
        if len(sel) and "MEASURE_name" in sel.columns:
            sel = _prefer(sel, "MEASURE_name", measures)
        sel = _prefer(sel, "TSEST_name", ["Seasonally Adjusted", "Original", "Trend"])
        sel = _prefer(sel, "PRICE_ADJUSTMENT_name",
                      ["Chain Volume Measures", "Current Price"])
        if sel.empty:
            log_gap(SOURCE, series_id, f"no rows for measures {measures} in {flow}")
            continue
        dupes = sel["TIME_PERIOD"].duplicated()
        if dupes.any():
            raise RuntimeError(f"ABS {flow}: {series_id} still ambiguous after "
                               f"filters; sample:\n{sel[dupes].head(5)}")
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
