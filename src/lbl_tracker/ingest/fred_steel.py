"""FRED: US Census M3 new orders, iron & steel mills (monthly).

Uses the FRED API when FRED_API_KEY is set (preferred, per config). The
series id is configured in config.yaml (fred.series.steel_new_orders) and
verified against the live endpoint - see SOURCES.md. Without a key, the
public fredgraph.csv download for the same series id is used so the series
still ingests; the API path takes over once the key is provided.
"""
from __future__ import annotations

import io
import logging
import os

import pandas as pd

from ..config import cfg
from ..http import get
from ..store import now_utc, write_observations

log = logging.getLogger("lbl_tracker.fred")

SOURCE = "fred_steel"
API_URL = "https://api.stlouisfed.org/fred/series/observations"
CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"


def fred_series_id() -> str:
    return cfg("fred", "series", "steel_new_orders", default="AISMNO")


def fetch_api(series: str, api_key: str) -> pd.DataFrame:
    resp = get(API_URL, params={
        "series_id": series, "api_key": api_key, "file_type": "json",
        "observation_start": "1990-01-01",
    })
    obs = resp.json().get("observations", [])
    if not obs:
        raise RuntimeError(f"FRED API returned no observations for {series}")
    df = pd.DataFrame(obs)
    df["value"] = pd.to_numeric(df["value"], errors="coerce")  # '.' == missing
    return pd.DataFrame({
        "date": pd.to_datetime(df["date"]),
        "value": df["value"],
        "source_url": f"{API_URL}?series_id={series}",
    })


def fetch_csv(series: str) -> pd.DataFrame:
    resp = get(CSV_URL, params={"id": series})
    df = pd.read_csv(io.BytesIO(resp.content))
    if df.shape[1] < 2 or df.empty:
        raise RuntimeError(f"fredgraph.csv empty for {series}")
    date_col, value_col = df.columns[0], df.columns[1]
    return pd.DataFrame({
        "date": pd.to_datetime(df[date_col]),
        "value": pd.to_numeric(df[value_col], errors="coerce"),
        "source_url": f"{CSV_URL}?id={series}",
    })


def fetch() -> pd.DataFrame:
    series = fred_series_id()
    api_key = os.environ.get("FRED_API_KEY", "").strip()
    if api_key:
        df = fetch_api(series, api_key)
    else:
        log.warning("FRED_API_KEY not set; using public fredgraph.csv for %s", series)
        df = fetch_csv(series)
    df["series_id"] = "fred.steel_new_orders"
    df["retrieved_at"] = now_utc()
    return df


def ingest() -> dict:
    return write_observations(SOURCE, fetch())
