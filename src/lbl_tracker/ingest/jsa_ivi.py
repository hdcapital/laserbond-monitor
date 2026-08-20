"""Jobs & Skills Australia Internet Vacancy Index - 4-digit occupations.

The IVI detailed-occupation workbook (monthly, ANZSCO4 x state) is
published as XLSX on jobsandskills.gov.au; the link moves, so it is
discovered from the IVI data page each run.

Series stored:
  jsa.ivi.<anzsco4>.<state>     monthly vacancies per occupation/state
  jsa.ivi_trades_tightness      AUST sum over tracked trades occupations
"""
from __future__ import annotations

import io
import logging
import re
from urllib.parse import urljoin

import pandas as pd
from bs4 import BeautifulSoup

from ..config import cfg
from ..http import get, make_session
from ..store import now_utc, write_observations

log = logging.getLogger("lbl_tracker.jsa")

SOURCE = "jsa_ivi"
PAGES = [
    "https://www.jobsandskills.gov.au/data/internet-vacancy-index",
    "https://www.jobsandskills.gov.au/work/internet-vacancy-index",
]
LINK_PAT = re.compile(r"(detailed occupation|ivi.?data.*occupation|4.?digit)", re.I)

# jobsandskills.gov.au sits behind a WAF that stalls plain library
# user-agents (read-timeouts observed in CI); browser-like headers get
# normal responses.
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-AU,en;q=0.8",
}


def jsa_session():
    return make_session(extra_headers=BROWSER_HEADERS)


# data.gov.au's CKAN API lives under /data/ (bare /api/3 404s - verified
# live 2026-08-20).
DATA_GOV_CKAN = "https://data.gov.au/data/api/3/action"


def discover_via_data_gov(session) -> str | None:
    """JSA republishes the IVI on data.gov.au (CKAN) - far more reliable
    than the WAF-fronted jobsandskills.gov.au site."""
    try:
        resp = get(f"{DATA_GOV_CKAN}/package_search", session=session,
                   params={"q": "internet vacancy index", "rows": 20})
        results = resp.json()["result"]["results"]
    except Exception as exc:  # noqa: BLE001
        log.warning("jsa: data.gov.au search failed: %s", exc)
        return None
    candidates = []
    for pkg in results:
        for res in pkg.get("resources", []):
            name = f"{res.get('name', '')} {res.get('description', '')}"
            fmt = (res.get("format") or "").lower()
            if fmt in ("xlsx", "xls") and LINK_PAT.search(name):
                candidates.append((res.get("last_modified") or "", res.get("url")))
    if candidates:
        return sorted(candidates, reverse=True)[0][1]
    return None


def discover_workbook(session) -> str:
    url = discover_via_data_gov(session)
    if url:
        return url
    for page in PAGES:
        try:
            soup = BeautifulSoup(get(page, session=session).text, "lxml")
        except Exception as exc:  # noqa: BLE001
            log.warning("jsa: page %s failed: %s", page, exc)
            continue
        for a in soup.find_all("a", href=True):
            text = f"{a.get_text(' ', strip=True)} {a['href']}"
            if re.search(r"\.xlsx?($|\?)", a["href"], re.I) and LINK_PAT.search(text):
                return urljoin(page, a["href"])
    raise RuntimeError("jsa_ivi: detailed-occupation workbook link not found; run probe")


def parse_workbook(content: bytes, url: str) -> pd.DataFrame:
    occupations: dict = cfg("jsa", "occupations", default={})
    occ_codes = {str(code) for code in occupations}
    book = pd.read_excel(io.BytesIO(content), sheet_name=None, header=None, dtype=object)
    retrieved = now_utc()
    frames = []
    for sheet, raw in book.items():
        raw = raw.dropna(how="all")
        header_idx = None
        for i in range(min(len(raw), 12)):
            cells = [str(v).strip().lower() for v in raw.iloc[i].tolist()]
            if any("anzsco" in c for c in cells) and any("state" in c or c in
                                                         ("nsw", "vic") for c in cells):
                header_idx = i
                break
            # date columns present?
            parsed_dates = pd.to_datetime(pd.Series(raw.iloc[i].tolist()[3:]),
                                          errors="coerce")
            if parsed_dates.notna().sum() > 24 and any("anzsco" in c for c in cells):
                header_idx = i
                break
        if header_idx is None:
            continue
        header = raw.iloc[header_idx].tolist()
        data = raw.iloc[header_idx + 1:].reset_index(drop=True)
        data.columns = range(len(header))

        low = [str(h).strip().lower() for h in header]
        state_j = next((j for j, h in enumerate(low) if "state" in h), None)
        code_j = next((j for j, h in enumerate(low) if "anzsco" in h and "title" not in h), None)
        if code_j is None:
            continue
        date_cols = {}
        for j, h in enumerate(header):
            ts = pd.to_datetime(str(h), errors="coerce")
            if ts is None or pd.isna(ts):
                try:
                    ts = pd.to_datetime(h)
                except Exception:  # noqa: BLE001
                    continue
            if pd.notna(ts) and ts.year >= 2005:
                date_cols[j] = pd.Period(f"{ts.year}-{ts.month:02d}", freq="M") \
                    .end_time.normalize()
        if not date_cols:
            continue

        codes = data[code_j].astype(str).str.extract(r"(\d{4})")[0]
        mask = codes.isin(occ_codes)
        sel = data[mask]
        if sel.empty:
            continue
        for _, row in sel.iterrows():
            code = re.search(r"\d{4}", str(row[code_j])).group(0)
            state = str(row[state_j]).strip().upper() if state_j is not None else "AUST"
            state = {"AUSTRALIA": "AUST", "AUS": "AUST"}.get(state, state)
            vals = {d: pd.to_numeric(row[j], errors="coerce") for j, d in date_cols.items()}
            frames.append(pd.DataFrame({
                "series_id": f"jsa.ivi.{code}.{state.lower()}",
                "date": list(vals.keys()),
                "value": list(vals.values()),
                "source_url": url,
                "retrieved_at": retrieved,
            }))
        if frames:
            break
    if not frames:
        raise RuntimeError("jsa_ivi: workbook downloaded but no tracked occupation "
                           "rows parsed; run probe")
    df = pd.concat(frames, ignore_index=True)

    # Composite: national (AUST) sum across tracked trades occupations,
    # only for months where every tracked occupation reported.
    aust = df[df["series_id"].str.endswith(".aust")]
    if len(aust):
        counts = aust.groupby("date")["value"].agg(["count", "sum"])
        complete = counts[counts["count"] == len(occ_codes)]
        comp = pd.DataFrame({
            "series_id": "jsa.ivi_trades_tightness",
            "date": complete.index,
            "value": complete["sum"].values,
            "source_url": url,
            "retrieved_at": retrieved,
        })
        df = pd.concat([df, comp], ignore_index=True)
    return df


def fetch() -> pd.DataFrame:
    session = jsa_session()
    url = discover_workbook(session)
    log.info("jsa_ivi: workbook %s", url)
    return parse_workbook(get(url, session=session).content, url)


def ingest() -> dict:
    return write_observations(SOURCE, fetch())
