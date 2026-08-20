"""Caterpillar monthly dealer retail sales statistics from SEC EDGAR 8-Ks.

Caterpillar furnishes monthly dealer-statistics 8-Ks (Item 7.01) whose
exhibit 99 carries retail sales YoY changes by region and segment. The
filings are located via the data.sec.gov submissions API (the full-text
search host efts.sec.gov rejects even declared automated UAs from CI
runners - verified live 2026-08-20 - so it is only a fallback), each
exhibit fetched from the filing index and the Resource Industries row
parsed (3-month rolling YoY, %).

Series stored (monthly, percent YoY, negative = decline):
  cat.resource_industries_yoy_pct           World
  cat.resource_industries_yoy_pct.<region>  per region where present

EDGAR requires an identifying User-Agent; SEC_CONTACT_EMAIL is appended
when set.
"""
from __future__ import annotations

import logging
import re
import time

import pandas as pd

from ..http import get, make_session
from ..store import now_utc, write_observations

log = logging.getLogger("lbl_tracker.cat_edgar")

SOURCE = "cat_edgar"
CAT_CIK = "0000018230"
FTS_URL = "https://efts.sec.gov/LATEST/search-index"
ARCHIVES = "https://www.sec.gov/Archives/edgar/data"
QUERIES = ['"dealer statistics"', '"retail statistics"']
MAX_FILINGS = 120  # ~10 years of monthly filings

REGION_SLUGS = {
    "North America": "north_america", "Latin America": "latin_america",
    "EAME": "eame", "Asia/Pacific": "asia_pacific", "Asia Pacific": "asia_pacific",
    "World": "world", "Total": "world",
}
MONTH_PAT = re.compile(
    r"(?:months?\s+end(?:ed|ing)\s+|rolling\s+3\s+months?\s+end(?:ed|ing)\s+)"
    r"([A-Z][a-z]+)[\s,]+(\d{4})", re.I)
PCT_PAT = re.compile(r"\(?\s*(-?\d+(?:\.\d+)?)\s*\)?\s*%")


def _pct(cell: str) -> float | None:
    cell = str(cell).strip()
    if not cell or cell.lower() in ("nan", "none", "-", "—"):
        return None
    m = PCT_PAT.search(cell) or re.search(r"\(?\s*(-?\d+(?:\.\d+)?)\s*\)?$", cell)
    if not m:
        return None
    val = float(m.group(1))
    if "(" in cell and val > 0:
        val = -val
    return val


SUBMISSIONS_BASE = "https://data.sec.gov/submissions"


def search_filings(session) -> list[dict]:
    """CAT 8-K filings furnished under Item 7.01 (Reg FD - the monthly
    dealer statistics), newest first, via the submissions API."""
    doc = get(f"{SUBMISSIONS_BASE}/CIK{CAT_CIK}.json", session=session,
              sec=True).json()
    hits = []

    def collect(block: dict):
        forms = block.get("form", [])
        accs = block.get("accessionNumber", [])
        dates = block.get("filingDate", [])
        items = block.get("items", [""] * len(forms))
        for form, acc, date, item in zip(forms, accs, dates, items):
            if form == "8-K" and "7.01" in str(item):
                hits.append({"adsh": acc, "file_date": date})

    collect(doc.get("filings", {}).get("recent", {}))
    for extra in doc.get("filings", {}).get("files", []):
        if len(hits) >= MAX_FILINGS:
            break
        try:
            collect(get(f"{SUBMISSIONS_BASE}/{extra['name']}", session=session,
                        sec=True).json())
        except Exception as exc:  # noqa: BLE001
            log.warning("cat_edgar: archive page %s failed: %s", extra.get("name"), exc)
        time.sleep(0.15)
    log.info("cat_edgar: %d 8-K item-7.01 filings", len(hits))
    return hits[:MAX_FILINGS]


def exhibit_url(hit: dict, session) -> str | None:
    """Resolve the EX-99 exhibit document inside one filing."""
    adsh_nodash = hit["adsh"].replace("-", "")
    base = f"{ARCHIVES}/{int(CAT_CIK)}/{adsh_nodash}"
    index = get(f"{base}/index.json", session=session, sec=True).json()
    files = index.get("directory", {}).get("item", [])
    for entry in files:
        name = str(entry.get("name", ""))
        if re.search(r"ex[-_]?99", name, re.I) and name.lower().endswith(
                (".htm", ".html")):
            return f"{base}/{name}"
    # fallback: any non-index htm document
    for entry in files:
        name = str(entry.get("name", ""))
        if name.lower().endswith((".htm", ".html")) and "index" not in name.lower():
            return f"{base}/{name}"
    return None


def parse_exhibit(html: str, url: str) -> list[dict]:
    text = " ".join(BeautifulSoupText(html))
    month = MONTH_PAT.search(text)
    if not month:
        return []
    period = pd.Period(f"{month.group(2)}-{pd.to_datetime(month.group(1), format='%B').month:02d}",
                       freq="M").end_time.normalize()
    rows = []
    try:
        tables = pd.read_html(io_from(html))
    except ValueError:
        tables = []
    for table in tables:
        flat = table.astype(str)
        # A dealer-stats table mentions Resource Industries in some cell.
        mask = flat.apply(lambda col: col.str.contains("Resource Industries", case=False,
                                                       na=False))
        if not mask.any().any():
            continue
        header_like = [str(c) for c in table.columns]
        # Case A: segments as rows, regions as columns.
        ri_rows = flat[mask.any(axis=1)]
        for _, row in ri_rows.iterrows():
            for col_name, cell in row.items():
                region = _match_region(str(col_name))
                val = _pct(cell)
                if region and val is not None:
                    rows.append({"region": region, "value": val})
        # Case B: regions as rows, segments as columns.
        ri_cols = [c for c in table.columns if "resource" in str(c).lower()]
        if ri_cols:
            for _, row in flat.iterrows():
                region = _match_region(str(row.iloc[0]))
                val = _pct(row[ri_cols[0]])
                if region and val is not None:
                    rows.append({"region": region, "value": val})
        if rows:
            break
    out = []
    seen = set()
    for row in rows:
        if row["region"] in seen:
            continue
        seen.add(row["region"])
        sid = ("cat.resource_industries_yoy_pct" if row["region"] == "world"
               else f"cat.resource_industries_yoy_pct.{row['region']}")
        out.append({"series_id": sid, "date": period, "value": row["value"],
                    "source_url": url})
    return out


def _match_region(text: str) -> str | None:
    for name, slug in REGION_SLUGS.items():
        if name.lower() in text.lower():
            return slug
    return None


def BeautifulSoupText(html: str):
    from bs4 import BeautifulSoup
    return BeautifulSoup(html, "lxml").get_text(" ", strip=True).split()


def io_from(html: str):
    import io
    return io.StringIO(html)


def fetch() -> pd.DataFrame:
    session = make_session(sec=True)
    hits = search_filings(session)
    if not hits:
        raise RuntimeError("cat_edgar: no 8-K item-7.01 filings found")
    rows = []
    for hit in hits:
        try:
            url = exhibit_url(hit, session)
            if not url:
                log.info("cat_edgar: no exhibit in %s", hit["adsh"])
                continue
            parsed = parse_exhibit(get(url, session=session, sec=True).text, url)
            rows.extend(parsed)
            if not parsed:
                log.info("cat_edgar: no RI table in %s", url)
        except Exception as exc:  # noqa: BLE001
            log.warning("cat_edgar: %s failed: %s", hit.get("adsh"), exc)
        time.sleep(0.15)
    if not rows:
        raise RuntimeError("cat_edgar: filings found but no Resource Industries "
                           "figures parsed; run probe")
    df = pd.DataFrame(rows).drop_duplicates(["series_id", "date"], keep="first")
    df["retrieved_at"] = now_utc()
    return df


def ingest() -> dict:
    return write_observations(SOURCE, fetch())
