"""Queensland coal production - quarterly saleable tonnes, mine level.

Source: Queensland Open Data portal (CKAN, www.data.qld.gov.au), the
"Coal industry review statistical tables" datasets carry quarterly
saleable-production-by-mine spreadsheets. Resources are discovered through
the CKAN API each run so annual dataset roll-overs keep working.

Series stored:
  qld_coal.saleable_tonnes_total      quarterly, sum over mines (as published)
  qld_coal.mine.<slug>                quarterly, per mine
"""
from __future__ import annotations

import io
import logging
import re

import pandas as pd

from ..http import get, make_session
from ..store import now_utc, write_observations

log = logging.getLogger("lbl_tracker.qld_coal")

SOURCE = "qld_coal"
CKAN = "https://www.data.qld.gov.au/api/3/action"
SEARCH_QUERIES = ["coal industry review statistical tables"]
RESOURCE_PATTERN = re.compile(r"saleable", re.I)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(name).lower()).strip("_")


def discover_resources(session=None) -> list[dict]:
    """Find CSV/XLSX resources about saleable coal production."""
    session = session or make_session()
    found, seen = [], set()
    for query in SEARCH_QUERIES:
        resp = get(f"{CKAN}/package_search", session=session,
                   params={"q": query, "rows": 50})
        for pkg in resp.json()["result"]["results"]:
            for res in pkg.get("resources", []):
                name = f"{res.get('name', '')} {res.get('description', '')}"
                fmt = (res.get("format") or "").lower()
                if RESOURCE_PATTERN.search(name) and fmt in ("csv", "xlsx", "xls"):
                    if res["id"] in seen:
                        continue
                    seen.add(res["id"])
                    found.append({
                        "package": pkg.get("name"),
                        "name": res.get("name"),
                        "url": res.get("url"),
                        "format": fmt,
                        "created": res.get("created"),
                    })
    log.info("qld_coal: discovered %d saleable-production resources", len(found))
    return found


QUARTER_PAT = re.compile(r"(mar|jun|sep|dec)[a-z]*[\s\-.]*(\d{2,4})", re.I)


def _parse_period(text: str) -> pd.Timestamp | None:
    m = QUARTER_PAT.search(str(text))
    if not m:
        return None
    mon = {"mar": 3, "jun": 6, "sep": 9, "dec": 12}[m.group(1).lower()[:3]]
    year = int(m.group(2))
    if year < 100:
        year += 2000 if year < 70 else 1900
    return pd.Period(f"{year}-{mon:02d}", freq="M").end_time.normalize()


def parse_resource(url: str, fmt: str, session=None) -> pd.DataFrame:
    """Parse one saleable-production resource into long (mine, period, tonnes)."""
    resp = get(url, session=session)
    if fmt == "csv":
        tables = [pd.read_csv(io.BytesIO(resp.content), header=None, dtype=str)]
    else:
        book = pd.read_excel(io.BytesIO(resp.content), sheet_name=None, header=None, dtype=str)
        tables = list(book.values())

    rows = []
    for raw in tables:
        raw = raw.dropna(how="all").reset_index(drop=True)
        header_idx = None
        for i in range(min(len(raw), 15)):
            line = " ".join(str(v) for v in raw.iloc[i].tolist())
            if QUARTER_PAT.search(line) and ("mine" in line.lower() or i > 0):
                header_idx = i
                break
        if header_idx is None:
            continue
        header = raw.iloc[header_idx].tolist()
        period_cols = {j: _parse_period(h) for j, h in enumerate(header)}
        period_cols = {j: p for j, p in period_cols.items() if p is not None}
        if not period_cols:
            continue
        name_col = 0
        for i in range(header_idx + 1, len(raw)):
            mine = str(raw.iloc[i, name_col]).strip()
            if not mine or mine.lower() in ("nan", "total", "grand total"):
                continue
            for j, period in period_cols.items():
                val = str(raw.iloc[i, j]).replace(",", "").strip()
                try:
                    tonnes = float(val)
                except ValueError:
                    continue
                rows.append({"mine": mine, "date": period, "value": tonnes, "url": url})
    return pd.DataFrame(rows)


def fetch() -> pd.DataFrame:
    session = make_session()
    resources = discover_resources(session)
    if not resources:
        raise RuntimeError("qld_coal: no saleable-production resources found via CKAN")
    frames = []
    for res in resources:
        try:
            df = parse_resource(res["url"], res["format"], session)
            if len(df):
                frames.append(df)
        except Exception as exc:  # noqa: BLE001
            log.warning("qld_coal: resource %s failed: %s", res["name"], exc)
    if not frames:
        raise RuntimeError("qld_coal: resources found but none parsed; run probe")
    long = pd.concat(frames, ignore_index=True)
    long = long.drop_duplicates(["mine", "date"], keep="last")

    retrieved = now_utc()
    per_mine = pd.DataFrame({
        "series_id": "qld_coal.mine." + long["mine"].map(_slug),
        "date": long["date"],
        "value": long["value"],
        "source_url": long["url"],
        "retrieved_at": retrieved,
    })
    total = long.groupby("date").agg(value=("value", "sum"), url=("url", "first")).reset_index()
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
