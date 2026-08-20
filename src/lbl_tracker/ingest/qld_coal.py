"""Queensland coal production - quarterly saleable tonnes.

Source: Queensland Open Data portal (CKAN, www.data.qld.gov.au), dataset
"quarterly-coal-reports", resource "Quarterly Coal Production". Verified
live 2026-08-20: the workbook's 'Saleable Coal Production' sheet is a long
table (Year, Quarter, Mine Type, Coal type, Total Net Output (tonnes)),
2010 -> current, i.e. quarterly saleable production by MINE TYPE x COAL
TYPE. (Mine-level figures are only published annually, by financial year,
in the Coal Industry Review dataset - quarterly mine-level data does not
exist.) The portal fronts file downloads with an AWS WAF JavaScript
challenge, so the workbook is fetched with headless Chrome (Playwright).

Series stored (quarterly, tonnes):
  qld_coal.saleable_tonnes_total          sum over the published type splits
  qld_coal.type.<mine_type>.<coal_type>   e.g. qld_coal.type.open_cut.coking
"""
from __future__ import annotations

import io
import logging
import re
import time

import pandas as pd

from ..http import get, make_session
from ..store import now_utc, write_observations

log = logging.getLogger("lbl_tracker.qld_coal")

SOURCE = "qld_coal"
CKAN = "https://www.data.qld.gov.au/api/3/action"
SEARCH_QUERIES = ["quarterly coal reports"]
RESOURCE_PATTERN = re.compile(r"quarterly coal production", re.I)

# calendar quarters (data note: "Data through to 31 March 2026" matched
# the latest "2026 Q1" rows)
QUARTER_END_MONTH = {"Q1": 3, "Q2": 6, "Q3": 9, "Q4": 12}


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(name).lower()).strip("_")


def discover_resources(session=None) -> list[dict]:
    session = session or make_session()
    found, seen = [], set()
    for query in SEARCH_QUERIES:
        resp = get(f"{CKAN}/package_search", session=session,
                   params={"q": query, "rows": 50})
        for pkg in resp.json()["result"]["results"]:
            for res in pkg.get("resources", []):
                name = f"{res.get('name', '')} {res.get('description', '')}"
                if RESOURCE_PATTERN.search(name) and res["id"] not in seen:
                    seen.add(res["id"])
                    found.append({
                        "package": pkg.get("name"),
                        "name": res.get("name"),
                        "id": res["id"],
                        "url": res.get("url"),
                    })
    log.info("qld_coal: discovered %d quarterly-production resources", len(found))
    return found


def _download(url: str, session):
    """Plain fetch first (cheap); the portal usually answers HTTP 202 with
    an AWS WAF JS challenge, in which case headless Chrome retrieves it."""
    for attempt in range(2):
        resp = session.get(url, timeout=120)
        if resp.status_code == 200 and resp.content:
            return resp.content
        time.sleep(2)
    return _browser_download(url)


def _browser_download(url: str) -> bytes:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "qld_coal: the portal fronts downloads with an AWS WAF JS "
            "challenge; install the 'browser' extra (pip install -e "
            "'.[browser]') to fetch via headless Chrome") from exc
    from pathlib import Path
    with sync_playwright() as p:
        browser = None
        for launch in ({"channel": "chrome"}, {"channel": "chromium"}, {}):
            try:
                browser = p.chromium.launch(headless=True, **launch)
                break
            except Exception:  # noqa: BLE001
                continue
        if browser is None:
            raise RuntimeError("qld_coal: no Chrome/Chromium available for the "
                               "WAF-challenged download")
        try:
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()
            with page.expect_download(timeout=120000) as dl_info:
                try:
                    page.goto(url, timeout=120000)
                except Exception:  # noqa: BLE001 - goto "fails" when it becomes a download
                    pass
            content = Path(dl_info.value.path()).read_bytes()
            log.info("qld_coal: browser download %s -> %d bytes", url, len(content))
            return content
        finally:
            browser.close()


def parse_workbook(content: bytes, url: str) -> pd.DataFrame:
    """'Saleable Coal Production' long table -> (mine_type, coal_type,
    date, tonnes). Header verified live: row with Year / Quarter /
    Mine Type / Coal type / Total Net Output (tonnes)."""
    book = pd.read_excel(io.BytesIO(content), sheet_name=None, header=None, dtype=object)
    raw = None
    for name, sheet in book.items():
        if "saleable" in str(name).lower():
            raw = sheet
            break
    if raw is None:
        raise ValueError(f"no 'Saleable' sheet; sheets={list(book)}")
    header_idx = None
    for i in range(min(len(raw), 15)):
        cells = [str(v).strip().lower() for v in raw.iloc[i].tolist()]
        if "year" in cells and "quarter" in cells:
            header_idx = i
            break
    if header_idx is None:
        raise ValueError(f"no Year/Quarter header in Saleable sheet; head:\n"
                         f"{raw.head(10).to_string()[:1200]}")
    header = [str(v).strip().lower() for v in raw.iloc[header_idx].tolist()]
    data = raw.iloc[header_idx + 1:].copy()
    data.columns = header

    def col(key):
        for j, h in enumerate(header):
            if key in h:
                return data.iloc[:, j]
        raise ValueError(f"column {key!r} missing in {header}")

    year = pd.to_numeric(col("year"), errors="coerce")
    quarter = col("quarter").astype(str).str.strip().str.upper()
    mine_type = col("mine type").astype(str).str.strip()
    coal_type = col("coal type").astype(str).str.strip()
    tonnes = pd.to_numeric(col("output"), errors="coerce")

    frame = pd.DataFrame({"year": year, "quarter": quarter, "mine_type": mine_type,
                          "coal_type": coal_type, "value": tonnes})
    frame = frame[frame["quarter"].isin(QUARTER_END_MONTH) & frame["year"].notna()
                  & frame["value"].notna()]
    if frame.empty:
        raise ValueError("Saleable sheet parsed to zero rows")
    frame["date"] = [
        pd.Period(f"{int(y)}-{QUARTER_END_MONTH[q]:02d}", freq="M").end_time.normalize()
        for y, q in zip(frame["year"], frame["quarter"])]
    bad = frame["date"] > pd.Timestamp.now() + pd.Timedelta(days=95)
    if bad.any():
        raise ValueError(f"future-dated rows parsed - layout drift?\n{frame[bad].head()}")
    frame["url"] = url
    return frame


def fetch() -> pd.DataFrame:
    session = make_session()
    resources = discover_resources(session)
    if not resources:
        raise RuntimeError("qld_coal: no quarterly-production resource found via CKAN")
    res = resources[0]
    frame = parse_workbook(_download(res["url"], session), res["url"])

    retrieved = now_utc()
    per_type = pd.DataFrame({
        "series_id": ("qld_coal.type." + frame["mine_type"].map(_slug) + "."
                      + frame["coal_type"].map(_slug)),
        "date": frame["date"],
        "value": frame["value"],
        "source_url": frame["url"],
        "retrieved_at": retrieved,
    })
    total = frame.groupby("date").agg(value=("value", "sum"),
                                      url=("url", "first")).reset_index()
    total_rows = pd.DataFrame({
        "series_id": "qld_coal.saleable_tonnes_total",
        "date": total["date"],
        "value": total["value"],
        "source_url": total["url"],
        "retrieved_at": retrieved,
    })
    return pd.concat([total_rows, per_type], ignore_index=True)


def ingest() -> dict:
    return write_observations(SOURCE, fetch())
