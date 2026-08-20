"""Caterpillar monthly dealer retail sales statistics from SEC EDGAR 8-Ks.

Caterpillar furnishes monthly "dealer statistics" 8-Ks whose exhibit 99
carries retail sales YoY changes by region and segment. We locate the
filings with the EDGAR full-text search API (efts.sec.gov), fetch each
exhibit and parse the Resource Industries row (3-month rolling YoY, %).

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


def search_filings(session) -> list[dict]:
    """EDGAR full-text search for CAT dealer-statistics 8-Ks."""
    hits, seen = [], set()
    for query in QUERIES:
        frm = 0
        while frm < MAX_FILINGS * 2:
            resp = get(FTS_URL, session=session, sec=True, params={
                "q": query, "forms": "8-K", "ciks": CAT_CIK, "from": frm,
            })
            payload = resp.json()
            batch = payload.get("hits", {}).get("hits", [])
            if not batch:
                break
            for hit in batch:
                _id = hit.get("_id", "")
                if _id and _id not in seen:
                    seen.add(_id)
                    hits.append({
                        "id": _id,
                        "adsh": hit.get("_source", {}).get("adsh", _id.split(":")[0]),
                        "file_date": hit.get("_source", {}).get("file_date"),
                    })
            frm += len(batch)
            time.sleep(0.15)  # EDGAR politeness
        if hits:
            break
    log.info("cat_edgar: %d full-text hits", len(hits))
    return hits[:MAX_FILINGS]


def exhibit_url(hit: dict) -> str:
    adsh_nodash = hit["adsh"].replace("-", "")
    filename = hit["id"].split(":", 1)[1] if ":" in hit["id"] else ""
    return f"{ARCHIVES}/{int(CAT_CIK)}/{adsh_nodash}/{filename}"


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
        raise RuntimeError("cat_edgar: EDGAR full-text search returned no filings")
    rows = []
    for hit in hits:
        url = exhibit_url(hit)
        try:
            parsed = parse_exhibit(get(url, session=session, sec=True).text, url)
            rows.extend(parsed)
            if not parsed:
                log.info("cat_edgar: no RI table in %s", url)
        except Exception as exc:  # noqa: BLE001
            log.warning("cat_edgar: %s failed: %s", url, exc)
        time.sleep(0.15)
    if not rows:
        raise RuntimeError("cat_edgar: filings found but no Resource Industries "
                           "figures parsed; run probe")
    df = pd.DataFrame(rows).drop_duplicates(["series_id", "date"], keep="first")
    df["retrieved_at"] = now_utc()
    return df


def ingest() -> dict:
    return write_observations(SOURCE, fetch())
