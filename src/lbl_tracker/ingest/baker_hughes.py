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
# Anchor texts verified live 2026-08-20, e.g.:
#   "North America Rig Count Report - New Report" (current weekly workbook)
#   "North America Rig Count New Report (2013-Aug 2025)" (archive)
#   "North America Rotary Rig Count (Jan 2000 - Mar 2024)" (older archive)
#   "Worldwide Rig Count Report - New Report" / "(2013-Jul 2025)" /
#   "Worldwide Rig Count Jan 2007_Mar 2024"
# Workbooks are served from /static-files/<uuid> (no extension); some are
# .xlsb (pyxlsb required).
NA_LINK = re.compile(r"north.?americ.*rig.*count", re.I)
INTL_LINK = re.compile(r"(worldwide|international).*rig.*count", re.I)
INCLUDE = re.compile(r"new report|\(jan 2000|20\d\d\s*[-_]\s*[A-Za-z]*\s*20\d\d", re.I)
EXCLUDE = re.compile(r"pivot|by state|average|workover|through 2016|overview|"
                     r"iphone|app|faq", re.I)


def discover_files(session) -> dict:
    files = {"na": [], "intl": []}
    for page in PAGES:
        try:
            soup = BeautifulSoup(get(page, session=session).text, "lxml")
        except Exception as exc:  # noqa: BLE001
            log.warning("bh: page %s failed: %s", page, exc)
            continue
        for a in soup.find_all("a", href=True):
            href = a["href"]
            text = a.get_text(" ", strip=True)
            if "static-files" not in href and not re.search(r"\.xls[xbm]?($|\?)",
                                                            href, re.I):
                continue
            if EXCLUDE.search(text) or not INCLUDE.search(text):
                continue
            full = urljoin(page, href)
            if NA_LINK.search(text) and full not in files["na"]:
                files["na"].append(full)
            elif INTL_LINK.search(text) and full not in files["intl"]:
                files["intl"].append(full)
    log.info("bh files: %s", files)
    return files


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
    files = discover_files(session)
    retrieved = now_utc()
    frames, errors = [], []
    for url in files["na"]:
        try:
            frames.append(parse_na(get(url, session=session).content, url, retrieved))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"na {url}: {exc}")
    for url in files["intl"]:
        try:
            frames.append(parse_intl(get(url, session=session).content, url, retrieved))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"intl {url}: {exc}")
    if not files["na"]:
        log_gap(SOURCE, "bh.rigcount_na_total", "NA workbook links not found")
    if not files["intl"]:
        log_gap(SOURCE, "bh.rigcount_intl_total", "international workbook links not found")
    if errors:
        log.warning("baker_hughes parse errors: %s", errors)
    if not frames:
        raise RuntimeError(f"baker_hughes: no workbook parsed; errors={errors}")
    return pd.concat(frames, ignore_index=True)


def ingest() -> dict:
    return write_observations(SOURCE, fetch())
