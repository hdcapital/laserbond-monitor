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


def _long_table(raw: pd.DataFrame, needed: list[str]) -> pd.DataFrame | None:
    """Locate the header row of the long data table (verified live: header
    sits around row 10, e.g. Country/County/.../Year/Month/US_PublishDate/
    Rig Count Value) and return the rows below with lowercase headers."""
    for i in range(min(len(raw), 20)):
        cells = [str(v).strip().lower() for v in raw.iloc[i].tolist()]
        if all(any(need == c or need in c for c in cells) for need in needed):
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
                        f"{book[sheet].head(14).iloc[:, :14].to_string()[:1400]}")
    return RuntimeError(f"bh {kind} workbook not parsed ({url}); "
                        f"sheets={list(book)}\n" + "\n".join(dump))


def _excel_serial_dates(series: pd.Series) -> pd.Series:
    nums = pd.to_numeric(series, errors="coerce")
    as_serial = pd.to_datetime(nums, unit="D", origin="1899-12-30", errors="coerce")
    as_plain = pd.to_datetime(series, errors="coerce")
    return as_serial.where(nums.notna(), as_plain)


def parse_na(content: bytes, url: str, retrieved) -> pd.DataFrame:
    """Weekly US/Canada/North America totals.

    New-report workbooks (verified live 2026-08-20): sheet 'NAM Weekly' is a
    long table (Country x County x DrillFor x week) with a US_PublishDate
    column and 'Rig Count Value'; totals are the sum over the published
    granular rows, matching the workbook's own summary tab. Older archives
    carry 'US Oil & Gas Split' / 'Canada Oil & Gas Split' sheets with
    Excel-serial dates and a Total column."""
    book = _read_book(content)
    rows = []
    raw = book.get("NAM Weekly")
    if raw is not None:
        table = _long_table(raw, ["country", "year", "month"])
        if table is not None:
            dates = pd.to_datetime(_col(table, "publishdate", "publish date", "date"),
                                   errors="coerce")
            country = _col(table, "country").astype(str).str.strip().str.upper()
            value = pd.to_numeric(_col(table, "rig count", "value",
                                        exclude=("country",)),
                                  errors="coerce")
            frame = pd.DataFrame({"date": dates, "country": country, "value": value}) \
                .dropna(subset=["date", "value"])
            us = frame[frame["country"] == "UNITED STATES"].groupby("date")["value"].sum()
            canada = frame[frame["country"] == "CANADA"].groupby("date")["value"].sum()
            for series_id, ser in [("bh.rigcount_us_total", us),
                                   ("bh.rigcount_canada_total", canada)]:
                if len(ser):
                    rows.append(pd.DataFrame({
                        "series_id": series_id, "date": ser.index, "value": ser.values,
                        "source_url": url, "retrieved_at": retrieved}))
            na = us.add(canada, fill_value=None).dropna()
            if len(na):
                rows.append(pd.DataFrame({
                    "series_id": "bh.rigcount_na_total", "date": na.index,
                    "value": na.values, "source_url": url, "retrieved_at": retrieved}))
    else:
        # archive format: Date(serial), Oil, Gas, Misc, Total per country sheet
        parts = {}
        for sheet, series_id in [("US Oil & Gas Split", "bh.rigcount_us_total"),
                                 ("Canada Oil & Gas Split", "bh.rigcount_canada_total")]:
            raw = book.get(sheet)
            if raw is None:
                continue
            table = _long_table(raw, ["date", "total"])
            if table is None:
                continue
            dates = _excel_serial_dates(_col(table, "date"))
            total = pd.to_numeric(_col(table, "total"), errors="coerce")
            ser = pd.Series(total.values, index=dates.values).dropna()
            ser = ser[pd.notna(ser.index)]
            parts[series_id] = ser
            rows.append(pd.DataFrame({
                "series_id": series_id, "date": ser.index, "value": ser.values,
                "source_url": url, "retrieved_at": retrieved}))
        if len(parts) == 2:
            na = parts["bh.rigcount_us_total"].add(
                parts["bh.rigcount_canada_total"], fill_value=None).dropna()
            rows.append(pd.DataFrame({
                "series_id": "bh.rigcount_na_total", "date": na.index,
                "value": na.values, "source_url": url, "retrieved_at": retrieved}))
    if not rows:
        raise _sheet_error("NA", url, book, ["NAM Weekly", "US Oil & Gas Split"])
    return pd.concat(rows, ignore_index=True)


def parse_intl(content: bytes, url: str, retrieved) -> pd.DataFrame:
    """Monthly international total.

    New-report 'WW Monthly' is a long table (Region, Country, DrillFor,
    Location, [measure], Year, Month, value); international = sum over all
    non-North-America regions, matching the workbook's own summary. The
    2007-2024 archive is a per-year matrix with a 'Total Intl.' column."""
    book = _read_book(content)
    rows = []
    raw = book.get("WW Monthly")
    if raw is not None:
        table = _long_table(raw, ["region", "country", "year", "month"])
        if table is not None:
            year = pd.to_numeric(_col(table, "year"), errors="coerce")
            month = pd.to_numeric(_col(table, "month"), errors="coerce")
            region = _col(table, "region").astype(str).str.strip().str.casefold()
            value_col = _col(table, "rig count", "value", exclude=("country",))
            if value_col is None:  # archive variant: value is the col after month
                headers = list(table.columns)
                month_j = next(j for j, h in enumerate(headers) if "month" in h)
                value_col = table.iloc[:, month_j + 1]
            value = pd.to_numeric(value_col, errors="coerce")
            frame = pd.DataFrame({"year": year, "month": month, "region": region,
                                  "value": value}).dropna()
            intl = frame[frame["region"] != "north america"]
            grouped = intl.groupby(["year", "month"])["value"].sum()
            dates = [pd.Period(f"{int(y)}-{int(m):02d}", freq="M").end_time.normalize()
                     for (y, m) in grouped.index]
            rows.append(pd.DataFrame({
                "series_id": "bh.rigcount_intl_total", "date": dates,
                "value": grouped.values, "source_url": url, "retrieved_at": retrieved}))
    else:
        raw = book.get("Worldwide_Rigcount")
        if raw is not None:
            rows.extend(_parse_ww_matrix(raw, url, retrieved))
    if not rows:
        raise _sheet_error("intl", url, book, ["WW Monthly", "Worldwide_Rigcount"])
    return pd.concat(rows, ignore_index=True)


MONTH_ABBR = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct",
     "Nov", "Dec"], start=1)}


def _parse_ww_matrix(raw: pd.DataFrame, url: str, retrieved) -> list[pd.DataFrame]:
    """Archive layout: stacked per-year blocks - a row [.., 2024, Latin
    America, ..., Total Intl., Canada, U.S., Total World] then month rows."""
    out = []
    records = []
    for i in range(len(raw)):
        cells = [str(v).strip() for v in raw.iloc[i].tolist()]
        if "Total Intl." in cells:
            year = next((int(float(c)) for c in cells
                         if re.fullmatch(r"(19|20)\d\d(\.0)?", c)), None)
            if year is None:
                continue
            intl_j = cells.index("Total Intl.")
            for r in range(i + 1, min(i + 14, len(raw))):
                month_cell = str(raw.iloc[r, 1]).strip()[:3]
                if month_cell not in MONTH_ABBR:
                    break
                val = pd.to_numeric(raw.iloc[r, intl_j], errors="coerce")
                if pd.notna(val):
                    records.append((pd.Period(f"{year}-{MONTH_ABBR[month_cell]:02d}",
                                              freq="M").end_time.normalize(), float(val)))
    if records:
        frame = pd.DataFrame(records, columns=["date", "value"]) \
            .drop_duplicates("date", keep="first")
        out.append(pd.DataFrame({
            "series_id": "bh.rigcount_intl_total", "date": frame["date"],
            "value": frame["value"], "source_url": url, "retrieved_at": retrieved}))
    return out


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
