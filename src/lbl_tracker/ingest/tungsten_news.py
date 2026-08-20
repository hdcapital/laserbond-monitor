"""Tungsten policy flag monitor - Google News RSS keyword tracking.

THIS IS A PROXY, NOT A PRICE. No free tungsten/APT spot price exists, so
this series only flags policy/supply news events (export controls, APT,
China tungsten policy). Google News RSS returns a shallow window (~100
items per query), so history accumulates from first ingest; it is never
backfilled.

Stored:
  events/tungsten_flags        one row per headline (deduped)
  tungsten.flag_count          monthly count of flagged headlines
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import pandas as pd

from ..config import cfg
from ..http import get, make_session
from ..store import now_utc, read_events, stable_id, write_events, write_observations

log = logging.getLogger("lbl_tracker.tungsten")

SOURCE = "tungsten_news"
RSS_URL = "https://news.google.com/rss/search?q={query}&hl=en-AU&gl=AU&ceid=AU:en"


def fetch_flags() -> pd.DataFrame:
    session = make_session()
    keywords = cfg("tungsten", "keywords", default=[])
    if not keywords:
        raise RuntimeError("tungsten: no keywords configured")
    retrieved = now_utc()
    rows = []
    for keyword in keywords:
        url = RSS_URL.format(query=quote(f'"{keyword}"'))
        root = ET.fromstring(get(url, session=session).content)
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub = item.findtext("pubDate")
            if not title or not pub:
                continue
            try:
                published = parsedate_to_datetime(pub)
            except (TypeError, ValueError):
                continue
            ts = pd.Timestamp(published)
            ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
            rows.append({
                "id": stable_id(keyword, title, published.date().isoformat()),
                "published": ts.tz_localize(None),
                "title": title,
                "url": link,
                "keyword": keyword,
                "retrieved_at": retrieved,
            })
    if not rows:
        raise RuntimeError("tungsten: RSS returned no items for any keyword")
    return pd.DataFrame(rows).drop_duplicates("id")


def ingest() -> dict:
    flags = fetch_flags()
    stats = write_events("tungsten_flags", flags, key="id")

    # Monthly flag-count series over the full accumulated event store.
    all_flags = read_events("tungsten_flags")
    monthly = (all_flags.assign(month=pd.to_datetime(all_flags["published"])
                                .dt.to_period("M"))
               .groupby("month").size())
    retrieved = now_utc()
    obs = pd.DataFrame({
        "series_id": "tungsten.flag_count",
        "date": [p.end_time.normalize() for p in monthly.index],
        "value": monthly.values.astype(float),
        "source_url": "https://news.google.com/rss/search (keyword monitor, proxy)",
        "retrieved_at": retrieved,
    })
    obs_stats = write_observations(SOURCE, obs)
    return {**obs_stats, "flags_total": stats["rows_total"],
            "rows_written": stats["rows_written"]}
