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
    import os
    if not os.environ.get("SEC_CONTACT_EMAIL", "").strip():
        pytest.skip("cat_edgar pending SEC_CONTACT_EMAIL - EDGAR 403s requests "
                    "whose User-Agent lacks a contact address")
    import lbl_tracker.ingest.cat_edgar as cat
    old = cat.MAX_FILINGS
    cat.MAX_FILINGS = 8  # smoke: newest filings only
    try:
        df = cat.fetch()
    finally:
        cat.MAX_FILINGS = old
    assert_obs_schema(df)
    assert_fresh(df, "cat.resource_industries_yoy_pct", 75)


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
