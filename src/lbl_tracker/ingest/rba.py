"""RBA statistical tables: Index of Commodity Prices (I2) + AUD/USD.

The RBA publishes machine-readable CSVs per table. Format: metadata rows
(Title/Description/Frequency/Units/...) then a "Series ID" row naming each
column, then dated data rows. We parse by the Series ID row, never by
column position guesses.

Series stored:
  rba.commodity_index_aud   monthly, Index of Commodity Prices, AUD, all items
  rba.audusd                daily AUD/USD (current published window)
  rba.audusd_monthly        monthly average AUD/USD (long history, F11.1)
"""
from __future__ import annotations

import io
import logging

import pandas as pd

from ..http import get
from ..store import log_gap, now_utc, write_observations

log = logging.getLogger("lbl_tracker.rba")

SOURCE = "rba"
# Verified live 2026-08-20: F11.1 is the DAILY exchange-rate table, F11 is
# the MONTHLY one (title rows "A$1=USD", description "AUD/USD Exchange
# Rate"); I2 descriptions read "Index of commodity prices; All items; A$".
I2_URL = "https://www.rba.gov.au/statistics/tables/csv/i2-data.csv"
F11_MONTHLY_URL = "https://www.rba.gov.au/statistics/tables/csv/f11-data.csv"
FX_DAILY_URLS = [
    "https://www.rba.gov.au/statistics/tables/csv/f11.1-data.csv",
]


def parse_rba_csv(text: str) -> tuple[pd.DataFrame, dict]:
    """Return (data, meta) where data is indexed by date with series-id
    columns and meta maps series id -> its Title/Description string."""
    import csv
    lines = [row for row in csv.reader(io.StringIO(text))]
    width = max(len(row) for row in lines)
    lines = [row + [""] * (width - len(row)) for row in lines]
    raw = pd.DataFrame(lines, dtype=str).fillna("")
    id_rows = raw.index[raw[0].str.strip().str.lower() == "series id"]
    if not len(id_rows):
        raise ValueError("no 'Series ID' row found in RBA csv")
    id_row = id_rows[0]
    series_ids = raw.iloc[id_row, 1:].tolist()
    label_rows = raw.index[raw[0].str.strip().str.lower().isin(["title", "description"])]
    meta = {}
    for offset, sid in enumerate(series_ids, start=1):
        if sid:
            meta[sid] = " | ".join(str(raw.iloc[r, offset]) for r in label_rows)
    data = raw.iloc[id_row + 1:].copy()
    data.columns = ["date"] + series_ids
    data = data[data["date"].str.strip() != ""]
    data["date"] = pd.to_datetime(data["date"], format="mixed", dayfirst=True)
    for col in series_ids:
        if col:
            data[col] = pd.to_numeric(data[col].str.replace(",", ""), errors="coerce")
    return data.set_index("date"), meta


def _pick_series(meta: dict, *keywords: str) -> str | None:
    for sid, title in meta.items():
        low = str(title).lower()
        if all(k.lower() in low for k in keywords):
            return sid
    return None


def _to_rows(data: pd.DataFrame, sid: str, series_id: str, url: str, retrieved) -> pd.DataFrame:
    sel = data[sid]
    return pd.DataFrame({
        "series_id": series_id,
        "date": sel.index,
        "value": sel.values,
        "source_url": url,
        "retrieved_at": retrieved,
    })


def fetch() -> pd.DataFrame:
    retrieved = now_utc()
    frames = []

    def pick_usd(meta):
        return (_pick_series(meta, "a$1=usd") or _pick_series(meta, "aud/usd")
                or ("FXRUSD" if "FXRUSD" in meta else None))

    # Commodity price index (I2), AUD terms, all items.
    data, meta = parse_rba_csv(get(I2_URL).text)
    sid = _pick_series(meta, "all items", "a$") or _pick_series(meta, "all items")
    if sid:
        frames.append(_to_rows(data, sid, "rba.commodity_index_aud", I2_URL, retrieved))
    else:
        log_gap(SOURCE, "rba.commodity_index_aud", f"no all-items AUD column in I2; meta={meta}")

    # Monthly average AUD/USD (long history, table F11).
    data, meta = parse_rba_csv(get(F11_MONTHLY_URL).text)
    sid = pick_usd(meta)
    if sid:
        frames.append(_to_rows(data, sid, "rba.audusd_monthly", F11_MONTHLY_URL, retrieved))
    else:
        log_gap(SOURCE, "rba.audusd_monthly", f"no USD column in F11; meta={meta}")

    # Daily AUD/USD (table F11.1, current published window).
    daily_done = False
    for url in FX_DAILY_URLS:
        try:
            data, meta = parse_rba_csv(get(url).text)
        except Exception as exc:  # noqa: BLE001
            log.warning("daily FX url %s failed: %s", url, exc)
            continue
        sid = pick_usd(meta)
        if sid:
            frames.append(_to_rows(data, sid, "rba.audusd", url, retrieved))
            daily_done = True
            break
    if not daily_done:
        log_gap(SOURCE, "rba.audusd", "no daily FX csv parsed; see FX_DAILY_URLS")

    if not frames:
        raise RuntimeError("RBA: no series parsed at all")
    # Blank cells (e.g. FX holidays) are kept as NULL rows - they are what
    # the source published, never filled.
    return pd.concat(frames, ignore_index=True)


def ingest() -> dict:
    return write_observations(SOURCE, fetch())
