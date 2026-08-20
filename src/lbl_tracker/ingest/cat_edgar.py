"""Caterpillar monthly dealer retail sales statistics from SEC EDGAR 8-Ks.

Caterpillar furnished monthly retail-statistics 8-Ks (Item 7.01) with
dealer retail sales YoY changes by region and segment **until February
2017, when CAT discontinued the monthly series** (verified live: item-7.01
filings since then are quarterly earnings releases). This ingester
recovers the full published monthly history - Resources Industries block,
'UP x%' / 'DOWN x%' by region - via the data.sec.gov submissions API and
the filing archives. The series therefore ends at Jan 2017: it feeds the
backtest era of the Products Pulse and renormalises out of current months.

Series stored (monthly, percent YoY, negative = decline):
  cat.resource_industries_yoy_pct           World
  cat.resource_industries_yoy_pct.<region>  per region where present

EDGAR requires an identifying User-Agent in the documented shape
("company contact@email") - SEC_CONTACT_EMAIL supplies the contact; a UA
containing a URL trips SEC's automated-tool filter (verified live).
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
ARCHIVES = "https://www.sec.gov/Archives/edgar/data"
SUBMISSIONS_BASE = "https://data.sec.gov/submissions"
# 283 item-7.01 8-Ks exist 2004->current (verified live); monthly
# dealer-statistics filings ended Feb 2017, later ones are quarterly
# earnings releases that parse to nothing.
MAX_FILINGS = 400

REGION_SLUGS = {
    "North America": "north_america", "Latin America": "latin_america",
    "EAME": "eame", "Asia/Pacific": "asia_pacific", "Asia Pacific": "asia_pacific",
    "World": "world",
}
UPDOWN_PAT = re.compile(r"^(up|down)\s+(\d+(?:\.\d+)?)\s*%$", re.I)


def _updown(cell: str) -> float | None:
    """'UP 26%' -> 26.0, 'DOWN 8%' -> -8.0, 'FLAT'/'UNCHANGED' -> 0."""
    text = str(cell).strip()
    m = UPDOWN_PAT.match(text)
    if m:
        val = float(m.group(2))
        return -val if m.group(1).lower() == "down" else val
    if re.fullmatch(r"(?i)flat|unchanged", text):
        return 0.0
    return None


def _match_region(text: str) -> str | None:
    for name, slug in REGION_SLUGS.items():
        if name.lower() in str(text).lower():
            return slug
    return None


def parse_exhibit(html: str, url: str) -> list[dict]:
    """Monthly dealer-statistics layout (verified against the live 2016/17
    filings): a table block headed by a 'Resources Industries' row whose
    remaining columns are month labels ('January 2017', ...), followed by
    region rows valued 'UP x%' / 'DOWN x%'. Each filing carries the
    current month plus the two prior months. Quarterly earnings releases
    (the only 7.01 filings since CAT discontinued the monthly series in
    Feb 2017) contain no such block and parse to []."""
    import io as _io
    try:
        tables = pd.read_html(_io.StringIO(html))
    except ValueError:
        return []
    out, seen = [], set()
    for table in tables:
        flat = table.fillna("").astype(str)  # pandas>=3: astype(str) keeps NaN
        for ridx in range(len(flat)):
            header = flat.iloc[ridx, 0].strip()
            if not re.fullmatch(r"(?i)resources?\s+industries", header):
                continue
            months = {}
            for c in range(1, flat.shape[1]):
                label = flat.iloc[ridx, c].strip()
                try:
                    ts = pd.to_datetime("1 " + label)
                except (ValueError, TypeError):
                    continue
                if 2000 <= ts.year <= 2100:
                    months[c] = pd.Period(f"{ts.year}-{ts.month:02d}",
                                          freq="M").end_time.normalize()
            if not months:
                continue
            for r in range(ridx + 1, min(ridx + 8, len(flat))):
                region = _match_region(flat.iloc[r, 0])
                if region is None:
                    break
                for c, period in months.items():
                    val = _updown(flat.iloc[r, c])
                    key = (region, period)
                    if val is None or key in seen:
                        continue
                    seen.add(key)
                    sid = ("cat.resource_industries_yoy_pct" if region == "world"
                           else f"cat.resource_industries_yoy_pct.{region}")
                    out.append({"series_id": sid, "date": period, "value": val,
                                "source_url": url})
            if out:
                return out
    return out


def search_filings(session) -> list[dict]:
    """CAT 8-K filings furnished under Item 7.01 (Reg FD), newest first,
    via the data.sec.gov submissions API (incl. older archive pages)."""
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
    """Resolve the EX-99 exhibit (or the 8-K document itself - the monthly
    retail-statistics filings embed the tables there) inside one filing."""
    adsh_nodash = hit["adsh"].replace("-", "")
    base = f"{ARCHIVES}/{int(CAT_CIK)}/{adsh_nodash}"
    index = get(f"{base}/index.json", session=session, sec=True).json()
    files = index.get("directory", {}).get("item", [])
    for entry in files:
        name = str(entry.get("name", ""))
        if re.search(r"ex[-_]?99", name, re.I) and name.lower().endswith(
                (".htm", ".html")):
            return f"{base}/{name}"
    for entry in files:
        name = str(entry.get("name", ""))
        if name.lower().endswith((".htm", ".html")) and "index" not in name.lower():
            return f"{base}/{name}"
    return None


def fetch() -> pd.DataFrame:
    from ..store import read_series
    session = make_session(sec=True)
    hits = search_filings(session)
    if not hits:
        raise RuntimeError("cat_edgar: no 8-K item-7.01 filings found")

    # Incremental: once the (discontinued) monthly history is in the store,
    # only look at filings newer than what is already covered.
    prior = read_series("cat.resource_industries_yoy_pct")
    cutoff = None
    if len(prior):
        cutoff = (pd.to_datetime(prior["date"]).max()
                  - pd.Timedelta(days=90)).date().isoformat()
        hits = [h for h in hits if str(h["file_date"]) >= cutoff]
        log.info("cat_edgar: %d filings newer than %s", len(hits), cutoff)

    rows = []
    for hit in hits:
        try:
            url = exhibit_url(hit, session)
            if not url:
                log.info("cat_edgar: no exhibit in %s", hit["adsh"])
                continue
            parsed = parse_exhibit(get(url, session=session, sec=True).text, url)
            rows.extend(parsed)
        except Exception as exc:  # noqa: BLE001
            log.warning("cat_edgar: %s failed: %s", hit.get("adsh"), exc)
        time.sleep(0.15)
    if not rows:
        if cutoff is not None:
            # discontinued series: no new monthly filings is the normal state
            log.info("cat_edgar: no new dealer-statistics filings (series "
                     "discontinued by CAT in Feb 2017)")
            return pd.DataFrame(columns=["series_id", "date", "value",
                                         "source_url", "retrieved_at"])
        raise RuntimeError("cat_edgar: filings found but no Resource Industries "
                           "figures parsed; run probe")
    df = pd.DataFrame(rows).drop_duplicates(["series_id", "date"], keep="first")
    df["retrieved_at"] = now_utc()
    return df


def ingest() -> dict:
    df = fetch()
    if df.empty:
        return {"rows_written": 0, "note": "no new filings (discontinued series)"}
    return write_observations(SOURCE, df)
