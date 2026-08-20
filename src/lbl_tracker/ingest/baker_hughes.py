"""Baker Hughes rig counts - weekly North America + monthly international.

Baker Hughes publishes XLSX workbooks on rigcount.bakerhughes.com. The
download links are discovered from the page each run (filenames change).

Series stored:
  bh.rigcount_na_total       weekly, US + Canada rotary rigs
  bh.rigcount_us_total       weekly, US
  bh.rigcount_canada_total   weekly, Canada
  bh.rigcount_intl_total     monthly, international (ex NA)
"""
from __future__ import annotations

import io
import logging
import re
from urllib.parse import urljoin

import pandas as pd
from bs4 import BeautifulSoup

from ..http import get, make_session
from ..store import log_gap, now_utc, write_observations

log = logging.getLogger("lbl_tracker.baker_hughes")

SOURCE = "baker_hughes"
PAGES = [
    "https://rigcount.bakerhughes.com/na-rig-count",
    "https://rigcount.bakerhughes.com/intl-rig-count",
    "https://rigcount.bakerhughes.com/",
]
NA_LINK = re.compile(r"north.?america.*rotary.*rig", re.I)
INTL_LINK = re.compile(r"(worldwide|international).*rig", re.I)


def discover_links(session) -> dict:
    links = {"na": None, "intl": None}
    for page in PAGES:
        try:
            soup = BeautifulSoup(get(page, session=session).text, "lxml")
        except Exception as exc:  # noqa: BLE001
            log.warning("bh: page %s failed: %s", page, exc)
            continue
        for a in soup.find_all("a", href=True):
            href = a["href"]
            text = f"{a.get_text(' ', strip=True)} {href}"
            if not re.search(r"\.xls[xbm]?($|\?)", href, re.I):
                continue
            full = urljoin(page, href)
            if links["na"] is None and NA_LINK.search(text):
                links["na"] = full
            elif links["intl"] is None and INTL_LINK.search(text):
                links["intl"] = full
        if links["na"] and links["intl"]:
            break
    log.info("bh links: %s", links)
    return links


def _find_header(df: pd.DataFrame, required: list[str]) -> int | None:
    for i in range(min(len(df), 30)):
        line = [str(v).strip().lower() for v in df.iloc[i].tolist()]
        if all(any(req in cell for cell in line) for req in required):
            return i
    return None


def parse_na(content: bytes, url: str, retrieved) -> pd.DataFrame:
    book = pd.read_excel(io.BytesIO(content), sheet_name=None, header=None)
    rows = []
    for sheet, raw in book.items():
        header_idx = _find_header(raw, ["date", "u.s."]) or _find_header(raw, ["date", "us"])
        if header_idx is None:
            continue
        header = [str(v).strip().lower() for v in raw.iloc[header_idx].tolist()]
        data = raw.iloc[header_idx + 1:].copy()
        data.columns = header

        def col(*keys):
            for j, h in enumerate(header):
                if any(k in h for k in keys):
                    return data.iloc[:, j]
            return None

        dates = pd.to_datetime(col("date"), errors="coerce")
        us = pd.to_numeric(col("u.s.", "us"), errors="coerce")
        canada = pd.to_numeric(col("canada"), errors="coerce")
        mask = dates.notna()
        for series_id, vals in [("bh.rigcount_us_total", us),
                                ("bh.rigcount_canada_total", canada)]:
            if vals is None:
                continue
            rows.append(pd.DataFrame({
                "series_id": series_id, "date": dates[mask], "value": vals[mask],
                "source_url": url, "retrieved_at": retrieved}))
        if us is not None and canada is not None:
            rows.append(pd.DataFrame({
                "series_id": "bh.rigcount_na_total", "date": dates[mask],
                "value": (us + canada)[mask], "source_url": url,
                "retrieved_at": retrieved}))
        if rows:
            break
    if not rows:
        raise RuntimeError(f"bh NA workbook not parsed; sheets={list(book)}")
    return pd.concat(rows, ignore_index=True)


def parse_intl(content: bytes, url: str, retrieved) -> pd.DataFrame:
    book = pd.read_excel(io.BytesIO(content), sheet_name=None, header=None)
    rows = []
    for sheet, raw in book.items():
        header_idx = _find_header(raw, ["date", "total"]) or _find_header(raw, ["year", "total"])
        if header_idx is None:
            continue
        header = [str(v).strip().lower() for v in raw.iloc[header_idx].tolist()]
        data = raw.iloc[header_idx + 1:]
        date_j = next((j for j, h in enumerate(header) if "date" in h or "month" in h), None)
        total_j = next((j for j, h in enumerate(header)
                        if "total inter" in h or h == "total intl" or "world" in h
                        or h.strip() == "total"), None)
        if date_j is None or total_j is None:
            continue
        dates = pd.to_datetime(data.iloc[:, date_j], errors="coerce")
        vals = pd.to_numeric(data.iloc[:, total_j], errors="coerce")
        mask = dates.notna()
        if mask.sum() < 12:
            continue
        rows.append(pd.DataFrame({
            "series_id": "bh.rigcount_intl_total", "date": dates[mask],
            "value": vals[mask], "source_url": url, "retrieved_at": retrieved}))
        break
    if not rows:
        raise RuntimeError(f"bh intl workbook not parsed; sheets={list(book)}")
    return pd.concat(rows, ignore_index=True)


def fetch() -> pd.DataFrame:
    session = make_session()
    links = discover_links(session)
    retrieved = now_utc()
    frames = []
    if links["na"]:
        frames.append(parse_na(get(links["na"], session=session).content, links["na"], retrieved))
    else:
        log_gap(SOURCE, "bh.rigcount_na_total", "NA workbook link not found")
    if links["intl"]:
        frames.append(parse_intl(get(links["intl"], session=session).content,
                                 links["intl"], retrieved))
    else:
        log_gap(SOURCE, "bh.rigcount_intl_total", "international workbook link not found")
    if not frames:
        raise RuntimeError("baker_hughes: no workbook links discovered; run probe")
    return pd.concat(frames, ignore_index=True)


def ingest() -> dict:
    return write_observations(SOURCE, fetch())
