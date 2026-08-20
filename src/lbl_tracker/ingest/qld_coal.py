"""Queensland coal production - quarterly saleable tonnes, mine level.

Source: Queensland Open Data portal (CKAN, www.data.qld.gov.au), dataset
"quarterly-coal-reports", resource "Quarterly Coal Production" (mine-level,
2010 -> current; verified live 2026-08-20). The portal's file-download
endpoint answers HTTP 202 (staging) indefinitely for non-browser clients,
so rows are pulled through the CKAN datastore API instead, with the file
download kept as a fallback.

Series stored:
  qld_coal.saleable_tonnes_total      quarterly, sum over mines (as published)
  qld_coal.mine.<slug>                quarterly, per mine
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

QUARTER_PAT = re.compile(r"(mar|jun|sep|dec)[a-z]*[\s\-.]*(\d{2,4})", re.I)


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
                        "format": (res.get("format") or "").lower(),
                        "datastore_active": bool(res.get("datastore_active")),
                    })
    log.info("qld_coal: discovered %d quarterly-production resources", len(found))
    return found


DUMP_URL = "https://www.data.qld.gov.au/datastore/dump/{rid}"


def datastore_records(resource_id: str, session) -> pd.DataFrame:
    """Full-table CSV dump of a datastore resource, with the paged
    datastore_search JSON API as fallback."""
    try:
        resp = get(DUMP_URL.format(rid=resource_id), session=session, timeout=180)
        df = pd.read_csv(io.BytesIO(resp.content))
        if len(df):
            return df
    except Exception as exc:  # noqa: BLE001
        log.warning("qld_coal: datastore dump failed (%s); trying datastore_search", exc)
    rows, offset = [], 0
    while True:
        resp = get(f"{CKAN}/datastore_search", session=session, params={
            "resource_id": resource_id, "limit": 10000, "offset": offset})
        result = resp.json()["result"]
        batch = result.get("records", [])
        rows.extend(batch)
        offset += len(batch)
        if not batch or offset >= result.get("total", 0):
            break
    return pd.DataFrame(rows)


def _parse_period(text: str) -> pd.Timestamp | None:
    m = QUARTER_PAT.search(str(text))
    if not m:
        return None
    mon = {"mar": 3, "jun": 6, "sep": 9, "dec": 12}[m.group(1).lower()[:3]]
    year = int(m.group(2))
    if year < 100:
        year += 2000 if year < 70 else 1900
    return pd.Period(f"{year}-{mon:02d}", freq="M").end_time.normalize()


def records_to_long(df: pd.DataFrame, url: str) -> pd.DataFrame:
    """Datastore records -> long (mine, date, value). Field names are
    detected, not assumed: a mine column, a period column (values like
    'Mar-24' / 'March quarter 2024'), and a saleable/production tonnes
    column."""
    if df.empty:
        raise ValueError("datastore returned no records")
    cols = {str(c).lower(): c for c in df.columns}

    def find(*keys, exclude=()):
        for low, orig in cols.items():
            if any(k in low for k in keys) and not any(x in low for x in exclude):
                return orig
        return None

    mine_col = find("mine", exclude=("mineral",)) or find("operation", "site")
    period_col = None
    for low, orig in cols.items():
        sample = df[orig].astype(str).head(50)
        if sample.map(_parse_period).notna().mean() > 0.8:
            period_col = orig
            break
    value_col = find("saleable") or find("production", exclude=("type",)) \
        or find("tonn", exclude=("type",))
    if not (mine_col and period_col and value_col):
        raise ValueError(f"could not identify columns: mine={mine_col} "
                         f"period={period_col} value={value_col}; "
                         f"available={list(df.columns)}")

    out = pd.DataFrame({
        "mine": df[mine_col].astype(str).str.strip(),
        "date": df[period_col].map(_parse_period),
        "value": pd.to_numeric(df[value_col].astype(str).str.replace(",", ""),
                               errors="coerce"),
        "url": url,
    })
    out = out[out["date"].notna() & (out["mine"] != "")]
    out = out[~out["mine"].str.fullmatch(r"(?i)total|grand total|nan")]
    if out.empty:
        raise ValueError("no parseable datastore rows")
    return out


# --- fallback: direct file download (202 staging retried) -------------------

def _download(url: str, session):
    for attempt in range(8):
        resp = session.get(url, timeout=120)
        if resp.status_code == 200 and resp.content:
            return resp
        if resp.status_code != 202:
            resp.raise_for_status()
        time.sleep(min(2 ** attempt, 30))
    raise RuntimeError(f"qld_coal: {url} still HTTP 202 after retries")


def parse_workbook(content: bytes, url: str) -> pd.DataFrame:
    book = pd.read_excel(io.BytesIO(content), sheet_name=None, header=None, dtype=str)
    frames = []
    for _, raw in book.items():
        raw = raw.dropna(how="all").reset_index(drop=True)
        for i in range(min(len(raw), 15)):
            header = raw.iloc[i].tolist()
            period_cols = {j: _parse_period(h) for j, h in enumerate(header)}
            period_cols = {j: p for j, p in period_cols.items() if p is not None}
            if len(period_cols) < 4:
                continue
            for r in range(i + 1, len(raw)):
                mine = str(raw.iloc[r, 0]).strip()
                if not mine or mine.lower() in ("nan", "total", "grand total"):
                    continue
                for j, period in period_cols.items():
                    val = str(raw.iloc[r, j]).replace(",", "").strip()
                    try:
                        frames.append({"mine": mine, "date": period,
                                       "value": float(val), "url": url})
                    except ValueError:
                        continue
            break
    return pd.DataFrame(frames)


def fetch() -> pd.DataFrame:
    session = make_session()
    resources = discover_resources(session)
    if not resources:
        raise RuntimeError("qld_coal: no quarterly-production resource found via CKAN")
    long = None
    errors = []
    for res in resources:
        try:
            records = datastore_records(res["id"], session)
            long = records_to_long(records, res["url"])
            break
        except Exception as exc:  # noqa: BLE001
            errors.append(f"datastore {res['name']}: {exc}")
        try:
            long = parse_workbook(_download(res["url"], session).content, res["url"])
            if len(long):
                break
        except Exception as exc:  # noqa: BLE001
            errors.append(f"download {res['name']}: {exc}")
    if long is None or long.empty:
        raise RuntimeError(f"qld_coal: nothing parsed; attempts: {errors}")
    long = long.drop_duplicates(["mine", "date"], keep="last")

    retrieved = now_utc()
    per_mine = pd.DataFrame({
        "series_id": "qld_coal.mine." + long["mine"].map(_slug),
        "date": long["date"],
        "value": long["value"],
        "source_url": long["url"],
        "retrieved_at": retrieved,
    })
    total = long.groupby("date").agg(value=("value", "sum"),
                                     url=("url", "first")).reset_index()
    total_rows = pd.DataFrame({
        "series_id": "qld_coal.saleable_tonnes_total",
        "date": total["date"],
        "value": total["value"],
        "source_url": total["url"],
        "retrieved_at": retrieved,
    })
    return pd.concat([total_rows, per_mine], ignore_index=True)


def ingest() -> dict:
    return write_observations(SOURCE, fetch())
