"""Tungsten policy flag monitor - Google News RSS keyword tracking.

THIS IS A PROXY, NOT A PRICE. No free tungsten/APT spot price exists, so
this series only flags policy/supply news events (export controls, APT,
China tungsten policy). Google News RSS returns a shallow window (~100
items per query), so history accumulates from first ingest; it is never
backfilled.

Stored:
  events/tungsten_flags        one row per headline (deduped)
  tungsten.flag_count          monthly count of DISTINCT flagged headlines,
                               completed months only
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import pandas as pd

from ..config import cfg
from ..http import get, make_session
from ..store import (now_utc, purge_observations, read_events, stable_id, write_events,
                     write_observations)

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
    failures = []
    for keyword in keywords:
        url = RSS_URL.format(query=quote(f'"{keyword}"'))
        # Google News 503s bursts from datacenter IPs; pace the queries and
        # tolerate a keyword failing this run (events are deduped, so the
        # next run catches up - nothing is lost or invented).
        import time
        time.sleep(3)
        try:
            root = ET.fromstring(get(url, session=session).content)
        except Exception as exc:  # noqa: BLE001
            failures.append(keyword)
            from ..store import log_gap
            log_gap("tungsten_news", "tungsten.flag_count",
                    f"RSS fetch failed for {keyword!r}: {exc}")
            continue
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
        raise RuntimeError(f"tungsten: RSS returned no items for any keyword "
                           f"(failed: {failures})")
    return pd.DataFrame(rows).drop_duplicates("id")


SERIES_ID = "tungsten.flag_count"


def _naive(ts) -> pd.Timestamp:
    ts = pd.Timestamp(ts)
    return ts.tz_convert("UTC").tz_localize(None) if ts.tzinfo else ts


def monthly_counts(all_flags: pd.DataFrame, now) -> pd.Series:
    """Distinct headlines per COMPLETED month since monitoring began.

    * A headline matched by several keywords is stored once per keyword
      (the event id includes the keyword) but is one news event: count
      each (normalised title, publication day) once.
    * The current month is still accumulating, so it is never emitted - a
      part-month count would read as a collapse in flags.
    * Months before monitoring began would be undercounted (shallow RSS
      window), so they are never emitted either."""
    if all_flags.empty:
        return pd.Series(dtype=float)
    flags = all_flags.copy()
    flags["published"] = pd.to_datetime(flags["published"])
    flags["_title"] = (flags["title"].astype(str).str.lower()
                       .str.replace(r"\s+", " ", regex=True).str.strip())
    flags["_day"] = flags["published"].dt.date
    distinct = flags.drop_duplicates(["_title", "_day"])
    start_month = _naive(pd.to_datetime(all_flags["retrieved_at"]).min()).to_period("M")
    current = _naive(now).to_period("M")
    monthly = distinct.groupby(distinct["published"].dt.to_period("M")).size()
    keep = (monthly.index >= start_month) & (monthly.index < current)
    return monthly[keep].astype(float)


def ingest() -> dict:
    from ..store import read_series

    flags = fetch_flags()
    stats = write_events("tungsten_flags", flags, key="id")

    now = now_utc()
    monthly = monthly_counts(read_events("tungsten_flags"), now)
    # Earlier versions emitted the running month; remove any stored count
    # for a month that is not complete yet.
    month_start = _naive(now).to_period("M").start_time
    prior = read_series(SERIES_ID)
    open_months = [d for d in pd.to_datetime(prior["date"]) if d >= month_start]
    if open_months:
        purge_observations(SOURCE, SERIES_ID, "count for a month still in progress "
                           "(only completed months are emitted)", dates=open_months)
    if monthly.empty:
        return {"source": SOURCE, "rows_written": 0, "flags_total": stats["rows_total"],
                "events_written": stats["rows_written"]}
    obs = pd.DataFrame({
        "series_id": SERIES_ID,
        "date": [p.end_time.normalize() for p in monthly.index],
        "value": monthly.values,
        "source_url": "https://news.google.com/rss/search (keyword monitor, proxy)",
        "retrieved_at": now,
    })
    obs_stats = write_observations(SOURCE, obs)
    return {**obs_stats, "flags_total": stats["rows_total"],
            "events_written": stats["rows_written"]}
