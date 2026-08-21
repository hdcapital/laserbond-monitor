"""Live smoke tests - one per ingester. These hit the real endpoints and
FAIL LOUDLY on source drift: schema wrong, feed empty, or latest value
older than the source's publication cadence allows.

Run with: pytest -m live tests/smoke
"""
from __future__ import annotations

import pandas as pd
import pytest

pytestmark = pytest.mark.live

OBS_COLUMNS = {"series_id", "date", "value", "source_url", "retrieved_at"}


def assert_obs_schema(df: pd.DataFrame):
    assert OBS_COLUMNS.issubset(df.columns), f"missing columns: {OBS_COLUMNS - set(df.columns)}"
    assert len(df), "empty frame"
    assert df["source_url"].astype(str).str.startswith("http").all()
    assert df["retrieved_at"].notna().all()


def assert_fresh(df: pd.DataFrame, series_id: str, max_age_days: int):
    sel = df[(df["series_id"] == series_id) & df["value"].notna()]
    assert len(sel), f"{series_id}: no non-null values"
    last = pd.to_datetime(sel["date"]).max()
    age = (pd.Timestamp.now() - last).days
    assert age <= max_age_days, (f"{series_id}: latest value {last.date()} is "
                                 f"{age}d old (max {max_age_days}) - source drift?")


def test_rba():
    from lbl_tracker.ingest.rba import fetch
    df = fetch()
    assert_obs_schema(df)
    assert_fresh(df, "rba.commodity_index_aud", 75)
    assert_fresh(df, "rba.audusd_monthly", 75)
    assert_fresh(df, "rba.audusd", 30)
    # long history required for 5yr z-scores
    monthly = df[df["series_id"] == "rba.audusd_monthly"]
    assert (pd.Timestamp.now() - pd.to_datetime(monthly["date"]).min()).days > 365 * 10


def test_abs_capex():
    from lbl_tracker.ingest.abs_capex import fetch
    df = fetch()
    assert_obs_schema(df)
    assert_fresh(df, "abs.capex.mining_actual", 200)
    assert_fresh(df, "abs.capex.mining_expected", 200)


def test_abs_exploration():
    from lbl_tracker.ingest.abs_exploration import fetch
    df = fetch()
    assert_obs_schema(df)
    assert_fresh(df, "abs.exploration.metres_drilled_total", 200)
    # ABS no longer publishes metres drilled by state in the MIN_EXP flow
    # (verified 2026-08); state coverage comes from exploration expenditure.
    assert_fresh(df, "abs.exploration.expenditure_wa", 200)


def test_qld_coal():
    from lbl_tracker.ingest.qld_coal import fetch
    df = fetch()
    assert_obs_schema(df)
    assert_fresh(df, "qld_coal.saleable_tonnes_total", 400)
    assert df["series_id"].str.startswith("qld_coal.type.").sum() > 0, \
        "no mine-type/coal-type rows"
    assert pd.to_datetime(df["date"]).max() <= pd.Timestamp.now() + pd.Timedelta(days=95)
    total = df[df["series_id"] == "qld_coal.saleable_tonnes_total"]["value"]
    assert total.between(2e7, 9e7).all(), "quarterly total outside sane range (tonnes)"


def test_pilbara_ports():
    from lbl_tracker.ingest.pilbara_ports import fetch
    df = fetch()
    assert_obs_schema(df)
    assert_fresh(df, "pilbara.iron_ore_throughput_mt", 90)


def test_aisi():
    from lbl_tracker.ingest.aisi import fetch
    df = fetch()
    assert_obs_schema(df)
    assert_fresh(df, "aisi.capacity_utilisation_pct", 21)
    util = df[df["series_id"] == "aisi.capacity_utilisation_pct"]["value"]
    assert util.between(30, 100).all(), "utilisation out of sane range - parser drift?"


def test_fred_steel():
    from lbl_tracker.ingest.fred_steel import fetch
    df = fetch()
    assert_obs_schema(df)
    assert_fresh(df, "fred.steel_new_orders", 120)
    dates = pd.to_datetime(df["date"])
    assert (pd.Timestamp.now() - dates.min()).days > 365 * 10, "history too shallow"


def test_baker_hughes():
    from lbl_tracker.ingest.baker_hughes import fetch
    df = fetch()
    assert_obs_schema(df)
    assert_fresh(df, "bh.rigcount_na_total", 21)
    assert_fresh(df, "bh.rigcount_intl_total", 75)


def test_cat_edgar():
    """CAT discontinued the monthly dealer-statistics 8-Ks in early 2021, so
    freshness cannot be asserted; instead assert the historical monthly era
    still parses (the drift that matters is EDGAR access + layout)."""
    import os
    if not os.environ.get("SEC_CONTACT_EMAIL", "").strip():
        pytest.skip("cat_edgar pending SEC_CONTACT_EMAIL - EDGAR 403s requests "
                    "whose User-Agent lacks a contact address")
    import lbl_tracker.ingest.cat_edgar as cat
    from lbl_tracker.http import get, make_session
    session = make_session(sec=True)
    hits = cat.search_filings(session)
    assert len(hits) > 100, f"item-7.01 filing list too short ({len(hits)})"
    era = [h for h in hits if "2016-01-01" <= str(h["file_date"]) <= "2017-02-28"][:3]
    assert era, "no monthly-era (2016/17) filings found"
    rows = []
    for hit in era:
        url = cat.exhibit_url(hit, session)
        assert url, f"no exhibit for {hit}"
        rows += cat.parse_exhibit(get(url, session=session, sec=True).text, url)
    world = [r for r in rows if r["series_id"] == "cat.resource_industries_yoy_pct"]
    assert len(world) >= 3, f"no world Resource Industries figures parsed: {rows[:4]}"
    assert all(-100 <= r["value"] <= 100 for r in rows)
    assert max(r["date"] for r in rows) < pd.Timestamp("2017-06-01")


def test_jsa_ivi():
    from lbl_tracker.ingest.jsa_ivi import fetch
    df = fetch()
    assert_obs_schema(df)
    assert_fresh(df, "jsa.ivi_trades_tightness", 120)


def test_tungsten_news():
    from lbl_tracker.ingest.tungsten_news import fetch_flags
    flags = fetch_flags()
    assert {"id", "published", "title", "url", "keyword"}.issubset(flags.columns)
    assert len(flags), "RSS returned nothing for any keyword"


def test_asx_announcements():
    from lbl_tracker.http import make_session
    from lbl_tracker.ingest.asx_announcements import fetch_list, resolve_pdf_url
    session = make_session()
    for ticker in ("LBL", "EHL", "MSV", "MAD"):
        items = fetch_list(ticker, 5, session)
        assert items, f"{ticker}: announcement list empty"
        assert all(i["headline"] for i in items)
        assert all(i["date"] for i in items)
    from lbl_tracker.http import get
    pdf = resolve_pdf_url(fetch_list("LBL", 5, session)[0], session)
    assert pdf, "LBL: could not resolve announcement PDF url"
    content = get(pdf, session=session).content
    assert content[:4] == b"%PDF", f"LBL: {pdf} did not return a PDF"


def test_nsw_coal_exports():
    from lbl_tracker.ingest.nsw_coal_exports import fetch
    df = fetch()
    assert_obs_schema(df)
    # ABS trade data publishes ~5 weeks in arrears
    assert_fresh(df, "abs.merch_exp.nsw_coal_value", 100)
    vals = df["value"].dropna()
    assert len(vals) > 300, "expected 30+ years of monthly history"
    assert vals.between(5e4, 2e7).all(), "A$ thousand unit drift?"


def test_oem_orders_discovery():
    from lbl_tracker.ingest.oem_orders import fls_documents, weir_documents
    fls = fls_documents()
    assert len(fls) > 10, "FLSmidth report list shrank - page layout drift?"
    assert all(d["url"].endswith(".pdf") for d in fls)
    weir = weir_documents()
    assert len(weir) > 5, "Weir sitemap results articles missing - drift?"
