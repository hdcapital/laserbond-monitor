"""Shared helpers for the ABS Data API (api.data.abs.gov.au, SDMX)."""
from __future__ import annotations

import logging
from functools import lru_cache

import pandas as pd

from ..http import get, make_session

log = logging.getLogger("lbl_tracker.abs")

# The ABS Data API moved from api.data.abs.gov.au to data.api.abs.gov.au
# (the old host no longer resolves - verified 2026-08); keep both so a
# future flip back keeps working.
BASES = ["https://data.api.abs.gov.au", "https://api.data.abs.gov.au"]

STRUCTURE_JSON = "application/vnd.sdmx.structure+json"
DATA_JSON = "application/vnd.sdmx.data+json"


@lru_cache(maxsize=1)
def resolve_base() -> str:
    last = None
    for base in BASES:
        try:
            session = make_session(extra_headers={"Accept": STRUCTURE_JSON})
            get(f"{base}/rest/dataflow/ABS?detail=allstubs", session=session, timeout=90)
            return base
        except Exception as exc:  # noqa: BLE001
            last = exc
    raise RuntimeError(f"no ABS API base reachable: {last}")


@lru_cache(maxsize=1)
def list_dataflows() -> pd.DataFrame:
    """All ABS dataflows (id, name) from the /dataflow endpoint."""
    session = make_session(extra_headers={"Accept": STRUCTURE_JSON})
    resp = get(f"{resolve_base()}/rest/dataflow/ABS?detail=allstubs", session=session)
    doc = resp.json()
    flows = doc.get("data", {}).get("dataflows", []) or doc.get("dataflows", [])
    rows = [{"id": f.get("id"), "name": f.get("name"), "version": f.get("version")}
            for f in flows]
    return pd.DataFrame(rows)


def find_dataflows(keyword: str) -> pd.DataFrame:
    flows = list_dataflows()
    mask = flows["name"].str.contains(keyword, case=False, na=False) | \
        flows["id"].str.contains(keyword, case=False, na=False)
    return flows[mask]


def get_data(flow_id: str, key: str = "all", params: dict | None = None) -> pd.DataFrame:
    """Fetch an SDMX-JSON dataset and return a long dataframe.

    Columns: TIME_PERIOD, value, plus one column per series dimension (by
    dimension id, values as codelist ids) and *_name columns with labels.
    """
    session = make_session(extra_headers={"Accept": DATA_JSON})
    url = f"{resolve_base()}/rest/data/ABS,{flow_id}/{key}"
    resp = get(url, session=session, params=params or {})
    doc = resp.json()
    data = doc.get("data", doc)
    structure = data.get("structures", [None])[0] if "structures" in data else data.get("structure")
    if structure is None:
        raise ValueError(f"unexpected SDMX-JSON shape for {flow_id}: keys={list(data)[:10]}")

    series_dims = structure["dimensions"]["series"]
    obs_dims = structure["dimensions"]["observation"]
    time_dim = next(d for d in obs_dims if d["id"] in ("TIME_PERIOD", "TIME"))
    time_values = [v["id"] for v in time_dim["values"]]

    rows = []
    datasets = data.get("dataSets", [])
    for dataset in datasets:
        for series_key, series in dataset.get("series", {}).items():
            idx = [int(i) for i in series_key.split(":")]
            dims = {}
            for pos, dim in zip(idx, series_dims):
                val = dim["values"][pos]
                dims[dim["id"]] = val.get("id")
                dims[dim["id"] + "_name"] = val.get("name")
            for obs_idx, obs in series.get("observations", {}).items():
                period = time_values[int(obs_idx)]
                value = obs[0] if obs else None
                rows.append({"TIME_PERIOD": period, "value": value, **dims})
    df = pd.DataFrame(rows)
    log.info("ABS %s: %d observations, dims=%s", flow_id, len(df),
             [d["id"] for d in series_dims])
    return df


def parse_time_period(period: str) -> pd.Timestamp:
    """ABS TIME_PERIOD to period-end timestamp. Supports 2024, 2024-Q2, 2024-05."""
    period = str(period)
    if "-Q" in period:
        return pd.Period(period.replace("-", ""), freq="Q").end_time.normalize()
    if len(period) == 7 and "-" in period:
        return pd.Period(period, freq="M").end_time.normalize()
    if len(period) == 4:
        return pd.Period(period, freq="Y").end_time.normalize()
    return pd.to_datetime(period)
