"""ABS 8412.0 Mineral Exploration - metres drilled by state (SDMX API)."""
from __future__ import annotations

import logging

import pandas as pd

from ..store import log_gap, now_utc, write_observations
from . import abs_sdmx

log = logging.getLogger("lbl_tracker.abs_exploration")

SOURCE = "abs_exploration"
# Verified live 2026-08-20: MIN_EXP "Mineral Exploration" with dims
# MEASURE / DEPOSIT_TYPE / MINERAL_TYPE / TSEST / REGION / FREQ.
FLOW_CANDIDATES = ["MIN_EXP"]
FLOW_KEYWORD = "mineral exploration"
SOURCE_URL = "https://data.api.abs.gov.au/rest/data/ABS,{flow}/all"

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


STATE_ABBREV = {"nsw": "nsw", "vic": "vic", "qld": "qld", "sa": "sa", "wa": "wa",
                "tas": "tas", "nt": "nt", "act": "act", "aus": "total",
                "aust": "total"}


def _region_slug(name: str) -> str | None:
    """Map a REGION_name label to a state slug, tolerant of abbreviations,
    'Total (X)' wrappers and case (MIN_EXP labels confirmed live to sit in
    a REGION dimension)."""
    low = str(name).strip().lower()
    for full, slug in STATE_SLUGS.items():
        if full.lower() in low:
            return slug
    compact = "".join(ch for ch in low if ch.isalpha())
    return STATE_ABBREV.get(compact)


def _prefer(df: pd.DataFrame, col: str, order: list[str]) -> pd.DataFrame:
    if col not in df.columns:
        return df
    vals = df[col].astype(str)
    for want in order:
        pick = df[vals.str.contains(want, case=False, na=False)]
        if len(pick):
            return pick
    return df


def fetch() -> pd.DataFrame:
    flow = resolve_flow()
    df = abs_sdmx.get_data(flow, "all", params={"startPeriod": "1990"})
    if df.empty:
        raise RuntimeError(f"ABS flow {flow} returned no observations")

    # Dimension values confirmed live 2026-08-20:
    # MEASURE_name: Expenditure / Metres drilled
    # DEPOSIT_TYPE_name: Existing / New / Total deposits
    # MINERAL_TYPE_name: per-mineral + 'Total' (exact match needed - the
    #   'Selected base metals total' label also contains "total")
    # REGION_name: Australia + the 7 states/territories (no ACT)
    # Verified against the live flow 2026-08-20: 'Metres drilled' is only
    # published for REGION == Australia (with an existing/new deposit
    # split); the state dimension is only published for the 'Expenditure'
    # measure. Both are stored: metres nationally, expenditure by state.
    if "MEASURE_name" in df.columns:
        metres = df[df["MEASURE_name"] == "Metres drilled"]
        spend = df[df["MEASURE_name"] == "Expenditure"]
    else:
        metres, spend = df, df.iloc[0:0]
    if metres.empty:
        raise RuntimeError(f"ABS {flow}: no 'Metres drilled' rows; measures="
                           f"{sorted(df.get('MEASURE_name', pd.Series()).dropna().unique())}")

    region_col = "REGION_name" if "REGION_name" in metres.columns else None
    if region_col is None:
        for col in metres.columns:
            if col.endswith("_name") and metres[col].map(_region_slug).notna().any():
                region_col = col
                break
    if region_col is None:
        raise RuntimeError(f"ABS {flow}: no region dimension; cols={list(metres.columns)}"
                           f"; sample values: "
                           f"{ {c: sorted(map(str, metres[c].dropna().unique()))[:10] for c in metres.columns if c.endswith('_name')} }")

    retrieved = now_utc()
    url = SOURCE_URL.format(flow=flow)
    rows = []
    for region_name in metres[region_col].dropna().unique():
        slug = _region_slug(region_name)
        if slug is None:
            continue
        sel = metres[metres[region_col] == region_name]
        # split availability differs by region (the flow is sparse), so
        # collapse deposit/mineral/adjustment per region, preferring totals
        sel = _prefer(sel, "DEPOSIT_TYPE_name", ["Total deposits"])
        sel = _prefer(sel, "MINERAL_TYPE_name", ["^Total$", "Total"])
        sel = _prefer(sel, "TSEST_name", ["Seasonally Adjusted", "Original", "Trend"])
        dupes = sel["TIME_PERIOD"].duplicated()
        if dupes.any():
            combos = sel[["DEPOSIT_TYPE_name", "MINERAL_TYPE_name", "TSEST_name"]] \
                .drop_duplicates().to_dict("records") \
                if "DEPOSIT_TYPE_name" in sel.columns else "?"
            log_gap(SOURCE, f"abs.exploration.{region_name}",
                    f"ambiguous after filters (combos={combos}); region skipped")
            continue
        series_id = ("abs.exploration.metres_drilled_total" if slug == "total"
                     else f"abs.exploration.metres_drilled_{slug}")
        rows.append(pd.DataFrame({
            "series_id": series_id,
            "date": sel["TIME_PERIOD"].map(abs_sdmx.parse_time_period),
            "value": sel["value"].values,
            "source_url": url,
            "retrieved_at": retrieved,
        }))
    # Exploration expenditure by state (the flow's only state-level measure).
    if len(spend) and region_col in spend.columns:
        for region_name in spend[region_col].dropna().unique():
            slug = _region_slug(region_name)
            if slug is None:
                continue
            sel = spend[spend[region_col] == region_name]
            sel = _prefer(sel, "DEPOSIT_TYPE_name", ["Total deposits"])
            sel = _prefer(sel, "MINERAL_TYPE_name", ["^Total$", "Total"])
            sel = _prefer(sel, "TSEST_name", ["Seasonally Adjusted", "Original", "Trend"])
            if sel["TIME_PERIOD"].duplicated().any():
                log_gap(SOURCE, f"abs.exploration.expenditure_{slug}",
                        "ambiguous after filters; region skipped")
                continue
            rows.append(pd.DataFrame({
                "series_id": f"abs.exploration.expenditure_{slug}",
                "date": sel["TIME_PERIOD"].map(abs_sdmx.parse_time_period),
                "value": sel["value"].values,
                "source_url": url,
                "retrieved_at": retrieved,
            }))
    if not rows:
        raise RuntimeError(f"ABS {flow}: no series extracted; regions="
                           f"{sorted(map(str, metres[region_col].dropna().unique()))}")
    return pd.concat(rows, ignore_index=True)


def ingest() -> dict:
    return write_observations(SOURCE, fetch())
