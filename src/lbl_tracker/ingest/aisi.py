"""AISI weekly raw-steel capability utilisation - scraped weekly release.

AISI publishes only the current week (plus a YTD figure) publicly, so
history accumulates in our store week by week from the first ingest
onward; there is no free backfill. Recent weeks are also recovered from
the AISI news listing of past weekly releases where available.

Series stored:
  aisi.capacity_utilisation_pct   weekly capability utilisation, percent
  aisi.raw_steel_production_kt    weekly production, thousand net tons
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urljoin

import pandas as pd
from bs4 import BeautifulSoup

from ..http import get, make_session
from ..store import now_utc, write_observations

log = logging.getLogger("lbl_tracker.aisi")

SOURCE = "aisi"
BASE = "https://www.steel.org"
DATA_PAGE = f"{BASE}/industry-data/"
NEWS_PATHS = [f"{BASE}/news/", f"{BASE}/newsroom/"]
RELEASE_TITLE = re.compile(r"raw steel production", re.I)

WEEK_PAT = re.compile(
    r"week ending(?: on)?\s+([A-Z][a-z]+ \d{1,2},? \d{4})", re.I)
PROD_PAT = re.compile(
    r"production was ([\d,]+(?:\.\d+)?)\s*(?:thousand )?net tons", re.I)
UTIL_PAT = re.compile(
    r"cap(?:ability|acity) utili[sz]ation rate of ([\d.]+)\s*percent", re.I)


def parse_release_text(text: str, url: str) -> dict | None:
    text = " ".join(text.split())
    week = WEEK_PAT.search(text)
    util = UTIL_PAT.search(text)
    if not (week and util):
        return None
    date = pd.to_datetime(week.group(1).replace(",", " "), format="mixed")
    out = {"date": date, "util": float(util.group(1)), "url": url}
    prod = PROD_PAT.search(text)
    if prod:
        tons = float(prod.group(1).replace(",", ""))
        # releases quote raw net tons (e.g. 1,743,000); normalise to kt
        out["prod_kt"] = tons / 1000.0 if tons > 100000 else tons
    return out


def collect_release_urls(session) -> list[str]:
    urls = []
    for path in [DATA_PAGE] + NEWS_PATHS:
        try:
            soup = BeautifulSoup(get(path, session=session).text, "lxml")
        except Exception as exc:  # noqa: BLE001
            log.warning("aisi: listing %s failed: %s", path, exc)
            continue
        for a in soup.find_all("a", href=True):
            if RELEASE_TITLE.search(a.get_text(" ", strip=True) or ""):
                urls.append(urljoin(BASE, a["href"]))
    return list(dict.fromkeys(urls))


def fetch() -> pd.DataFrame:
    session = make_session()
    parsed = []

    # The industry-data page itself carries the current week's figures.
    try:
        soup = BeautifulSoup(get(DATA_PAGE, session=session).text, "lxml")
        hit = parse_release_text(soup.get_text(" ", strip=True), DATA_PAGE)
        if hit:
            parsed.append(hit)
    except Exception as exc:  # noqa: BLE001
        log.warning("aisi: industry-data page failed: %s", exc)

    for url in collect_release_urls(session)[:30]:
        try:
            soup = BeautifulSoup(get(url, session=session).text, "lxml")
            hit = parse_release_text(soup.get_text(" ", strip=True), url)
            if hit:
                parsed.append(hit)
        except Exception as exc:  # noqa: BLE001
            log.warning("aisi: release %s failed: %s", url, exc)

    if not parsed:
        raise RuntimeError("aisi: no weekly release parsed; run probe")

    retrieved = now_utc()
    rows = []
    for hit in parsed:
        rows.append({"series_id": "aisi.capacity_utilisation_pct", "date": hit["date"],
                     "value": hit["util"], "source_url": hit["url"],
                     "retrieved_at": retrieved})
        if "prod_kt" in hit:
            rows.append({"series_id": "aisi.raw_steel_production_kt", "date": hit["date"],
                         "value": hit["prod_kt"], "source_url": hit["url"],
                         "retrieved_at": retrieved})
    return pd.DataFrame(rows).drop_duplicates(["series_id", "date"], keep="first")


def ingest() -> dict:
    return write_observations(SOURCE, fetch())
