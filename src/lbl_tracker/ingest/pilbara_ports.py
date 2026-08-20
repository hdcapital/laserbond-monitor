"""Pilbara Ports monthly throughput - scraped from monthly media statements.

Pilbara Ports (Port Hedland, Dampier, Ashburton) publishes a monthly trade
media statement. We walk the media-statement listing (paginated), fetch
each monthly-throughput statement and extract:

  pilbara.total_throughput_mt       total monthly throughput, million tonnes
  pilbara.iron_ore_throughput_mt    Port Hedland iron ore exports, million tonnes
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urljoin

import pandas as pd
from bs4 import BeautifulSoup

from ..http import get, make_session
from ..store import log_gap, now_utc, write_observations

log = logging.getLogger("lbl_tracker.pilbara")

SOURCE = "pilbara_ports"
BASE = "https://www.pilbaraports.com.au"
# Verified live 2026-08-20: the site is Kentico CMS; /news answers 200 and
# its canonical URL is the news,-media-and-statistics path.
LISTING_PATHS = [
    "/about-pilbara-ports/news,-media-and-statistics/news",
    "/news",
]
MAX_PAGES = 40

MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}
MONTHLY_TITLE = re.compile(r"(monthly|month(?:'s)?)\s+(trade|throughput)|throughput", re.I)

TOTAL_PAT = re.compile(
    r"total (?:monthly )?(?:port )?throughput of ([\d.,]+)\s*million tonnes", re.I)
HEDLAND_IRON_PAT = re.compile(
    r"port hedland[^.]*?iron ore export[s]?[^.]*?([\d.,]+)\s*million tonnes|"
    r"iron ore export[s]?[^.]*?([\d.,]+)\s*million tonnes[^.]*?port hedland", re.I)
MONTH_YEAR_PAT = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+(\d{4})", re.I)


def _num(text: str) -> float:
    return float(text.replace(",", ""))


def discover_listing(session) -> str:
    last_error = None
    for path in LISTING_PATHS:
        try:
            resp = get(urljoin(BASE, path), session=session)
            return resp.url
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    raise RuntimeError(f"pilbara_ports: no listing path worked; last: {last_error}")


NEWS_HREF = re.compile(r"/news/[^/?#]+/?$", re.I)


def list_statements(session) -> list[dict]:
    """Collect news-article links (hrefs under .../news/<slug>) across the
    paginated listing; the trade statements are identified when parsing."""
    listing_url = discover_listing(session)
    items, seen = [], set()
    for page in range(1, MAX_PAGES + 1):
        url = listing_url if page == 1 else f"{listing_url}?page={page}"
        try:
            soup = BeautifulSoup(get(url, session=session).text, "lxml")
        except Exception:  # noqa: BLE001
            break
        page_items = 0
        for a in soup.find_all("a", href=True):
            href = urljoin(BASE, a["href"])
            if not NEWS_HREF.search(href) or href in seen:
                continue
            title = " ".join(a.get_text(" ", strip=True).split())
            seen.add(href)
            items.append({"title": title, "url": href})
            page_items += 1
        if page_items == 0 and page > 1:
            break
    log.info("pilbara_ports: %d news articles found", len(items))
    return items


def parse_statement(url: str, title: str, session) -> list[dict]:
    soup = BeautifulSoup(get(url, session=session).text, "lxml")
    text = " ".join(soup.get_text(" ", strip=True).split())
    month_match = MONTH_YEAR_PAT.search(title) or MONTH_YEAR_PAT.search(text[:2000])
    if not month_match:
        return []
    period = pd.Period(f"{month_match.group(2)}-{MONTHS[month_match.group(1).lower()]:02d}",
                       freq="M").end_time.normalize()
    rows = []
    total = TOTAL_PAT.search(text)
    if total:
        rows.append({"series_id": "pilbara.total_throughput_mt", "date": period,
                     "value": _num(total.group(1)), "source_url": url})
    iron = HEDLAND_IRON_PAT.search(text)
    if iron:
        value = iron.group(1) or iron.group(2)
        rows.append({"series_id": "pilbara.iron_ore_throughput_mt", "date": period,
                     "value": _num(value), "source_url": url})
    if not rows and re.search(r"trade|throughput|tonnes", title, re.I):
        log_gap(SOURCE, "pilbara.total_throughput_mt",
                f"trade-looking statement matched no tonnage patterns: {url}")
    return rows


def fetch() -> pd.DataFrame:
    session = make_session()
    statements = list_statements(session)
    if not statements:
        raise RuntimeError("pilbara_ports: no news articles found on listing")
    trade_like = [s for s in statements
                  if re.search(r"trade|throughput|tonnes|export", s["title"], re.I)]
    to_parse = trade_like or statements  # anchor text can be empty on this CMS
    rows = []
    for item in to_parse:
        try:
            rows.extend(parse_statement(item["url"], item["title"], session))
        except Exception as exc:  # noqa: BLE001
            log.warning("pilbara_ports: %s failed: %s", item["url"], exc)
    if not rows:
        raise RuntimeError(f"pilbara_ports: {len(to_parse)} articles parsed but no "
                           "tonnage figures found; run probe")
    df = pd.DataFrame(rows)
    df["retrieved_at"] = now_utc()
    return df


def ingest() -> dict:
    return write_observations(SOURCE, fetch())
