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

# Verified against the live release text 2026-08-20. Each weekly release
# states three genuine datapoints: the current week, the same week a year
# earlier, and the previous week - all are captured.
DATE = r"([A-Z][a-z]+ \d{1,2},? \d{4})"
CUR_PAT = re.compile(
    rf"week ending(?: on)?\s+{DATE}\s*,?\s*domestic raw steel production was "
    rf"([\d,]+) net tons while the capability utili[sz]ation rate was "
    rf"([\d.]+)\s*percent", re.I)
YEAR_AGO_PAT = re.compile(
    rf"[Pp]roduction was ([\d,]+) net tons in the week ending\s+{DATE}\s*,?\s*"
    rf"while the capability utili[sz]ation(?: then)? was ([\d.]+)\s*percent", re.I)
PREV_WEEK_PAT = re.compile(
    rf"previous week ending\s+{DATE}\s*,?\s*when production was ([\d,]+) net "
    rf"tons and the rate of capability utili[sz]ation was ([\d.]+)\s*percent", re.I)


def _mk(date_s: str, tons_s: str, util_s: str, url: str) -> dict:
    return {
        "date": pd.to_datetime(date_s.replace(",", " "), format="mixed"),
        "util": float(util_s),
        "prod_kt": float(tons_s.replace(",", "")) / 1000.0,
        "url": url,
    }


def parse_release_text(text: str, url: str) -> list[dict]:
    text = " ".join(text.split())
    hits = []
    m = CUR_PAT.search(text)
    if m:
        hits.append(_mk(m.group(1), m.group(2), m.group(3), url))
    m = YEAR_AGO_PAT.search(text)
    if m:
        hits.append(_mk(m.group(2), m.group(1), m.group(3), url))
    m = PREV_WEEK_PAT.search(text)
    if m:
        hits.append(_mk(m.group(1), m.group(2), m.group(3), url))
    return hits


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
        parsed.extend(parse_release_text(soup.get_text(" ", strip=True), DATA_PAGE))
    except Exception as exc:  # noqa: BLE001
        log.warning("aisi: industry-data page failed: %s", exc)

    for url in collect_release_urls(session)[:30]:
        try:
            soup = BeautifulSoup(get(url, session=session).text, "lxml")
            parsed.extend(parse_release_text(soup.get_text(" ", strip=True), url))
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
        rows.append({"series_id": "aisi.raw_steel_production_kt", "date": hit["date"],
                     "value": hit["prod_kt"], "source_url": hit["url"],
                     "retrieved_at": retrieved})
    return pd.DataFrame(rows).drop_duplicates(["series_id", "date"], keep="first")


def ingest() -> dict:
    return write_observations(SOURCE, fetch())
