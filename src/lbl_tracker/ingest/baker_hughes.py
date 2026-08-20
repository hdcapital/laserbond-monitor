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


def _read_book(content: bytes) -> dict:
    return pd.read_excel(io.BytesIO(content), sheet_name=None, header=None)


def _find_table(raw: pd.DataFrame, date_pat: str, needed: list[str]) -> pd.DataFrame | None:
    """Locate a header row whose cells include a date-ish label plus all
    `needed` labels, and return the table below it with those headers."""
    for i in range(min(len(raw), 15)):
        cells = [str(v).strip().lower() for v in raw.iloc[i].tolist()]
        if not any(re.search(date_pat, c) for c in cells if c and c != "nan"):
            continue
        if all(any(need in c for c in cells) for need in needed):
            data = raw.iloc[i + 1:].copy()
            data.columns = cells
            return data
    return None


def _col(data: pd.DataFrame, *keys, exclude=()):
    for j, name in enumerate(data.columns):
        if any(k in name for k in keys) and not any(x in name for x in exclude):
            return data.iloc[:, j]
    return None


def _sheet_error(kind: str, url: str, book: dict, wanted: list[str]) -> RuntimeError:
    dump = []
    for sheet in wanted:
        if sheet in book:
            dump.append(f"--- {sheet} head ---\n"
                        f"{book[sheet].head(12).iloc[:, :14].to_string()[:1400]}")
    return RuntimeError(f"bh {kind} workbook not parsed ({url}); "
                        f"sheets={list(book)}\n" + "\n".join(dump))


def parse_na(content: bytes, url: str, retrieved) -> pd.DataFrame:
    """Weekly US/Canada/North America counts. New-report workbooks carry a
    'NAM Weekly' long table; older archives carry per-country split sheets."""
    book = _read_book(content)
    rows = []
    for sheet in ("NAM Weekly", "NAM Monthly"):
        raw = book.get(sheet)
        if raw is None:
            continue
        table = (_find_table(raw, r"date|week", ["country", "count"])
                 or _find_table(raw, r"date|week", ["count"])
                 or _find_table(raw, r"date|week", ["u.s", "canada"]))
        if table is None:
            continue
        dates = pd.to_datetime(_col(table, "date", "week"), errors="coerce")
        country = _col(table, "country", "location")
        count = _col(table, "count", "rig", exclude=("chg", "%", "change"))
        if count is None:
            continue
        count = pd.to_numeric(count, errors="coerce")
        if country is not None:
            frame = pd.DataFrame({"date": dates, "country": country.astype(str).str.strip(),
                                  "value": count}).dropna(subset=["date", "value"])
            for label, series_id in [("United States", "bh.rigcount_us_total"),
                                     ("Canada", "bh.rigcount_canada_total"),
                                     ("North America", "bh.rigcount_na_total")]:
                sel = frame[frame["country"].str.casefold() == label.casefold()]
                sel = sel.groupby("date")["value"].sum()
                if len(sel):
                    rows.append(pd.DataFrame({
                        "series_id": series_id, "date": sel.index, "value": sel.values,
                        "source_url": url, "retrieved_at": retrieved}))
        else:
            us = pd.to_numeric(_col(table, "u.s", "us "), errors="coerce")
            canada = pd.to_numeric(_col(table, "canada"), errors="coerce")
            mask = dates.notna()
            if us is not None and canada is not None:
                rows.append(pd.DataFrame({
                    "series_id": "bh.rigcount_na_total", "date": dates[mask],
                    "value": (us + canada)[mask], "source_url": url,
                    "retrieved_at": retrieved}))
        if rows:
            break
    if not rows:
        raise _sheet_error("NA", url, book, ["NAM Weekly", "NAM Monthly",
                                             "US Oil & Gas Split"])
    return pd.concat(rows, ignore_index=True)


def parse_intl(content: bytes, url: str, retrieved) -> pd.DataFrame:
    """Monthly international count from the 'WW Monthly' long table."""
    book = _read_book(content)
    rows = []
    for sheet in ("WW Monthly", "Worldwide_Rigcount"):
        raw = book.get(sheet)
        if raw is None:
            continue
        table = (_find_table(raw, r"date|month", ["country", "count"])
                 or _find_table(raw, r"date|month", ["region", "count"])
                 or _find_table(raw, r"date|month", ["count"]))
        if table is None:
            continue
        dates = pd.to_datetime(_col(table, "date", "month"), errors="coerce")
        entity = _col(table, "country", "region", "location")
        count = pd.to_numeric(_col(table, "count", "rig", exclude=("chg", "%", "change")),
                              errors="coerce")
        if count is None:
            continue
        frame = pd.DataFrame({"date": dates, "value": count})
        if entity is not None:
            frame["entity"] = entity.astype(str).str.strip().str.casefold()
            sel = frame[frame["entity"] == "international"]
            if sel.empty:
                continue
            sel = sel.dropna(subset=["date", "value"]).groupby("date")["value"].sum()
        else:
            sel = frame.dropna(subset=["date", "value"]).set_index("date")["value"]
        if len(sel) < 12:
            continue
        rows.append(pd.DataFrame({
            "series_id": "bh.rigcount_intl_total", "date": sel.index,
            "value": sel.values, "source_url": url, "retrieved_at": retrieved}))
        break
    if not rows:
        raise _sheet_error("intl", url, book, ["WW Monthly", "Worldwide_Rigcount"])
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
