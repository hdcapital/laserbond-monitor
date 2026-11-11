"""Data-integrity rules - synthetic text/frames only, never the real store."""
import json

import pandas as pd
import pytest

from lbl_tracker.ingest import oem_orders as oem

WEIR = ("Group orders were down 3% on a reported basis but up 4% at constant "
        "currency. Minerals orders increased 9%, with aftermarket orders up 15% "
        "and original equipment orders 2.5% lower. ESCO orders fell 6%.")


@pytest.mark.parametrize("value,near,ok", [
    (4, (r"\border",), True),            # "up 4%"
    (-4, (r"\border",), False),          # sign must match
    (-3, (r"\border",), True),           # "down 3%"
    (3, (r"\border",), False),           # sign-blind check used to accept this
    (5, (r"\border",), False),           # "5%" must not match inside "15%"/"2.5%"
    (-2.5, (r"\border",), True),         # "2.5% lower"
    (-6, (r"\border",), True),           # "fell 6%"
    (9, (r"\bminerals\b", r"\border"), True),
    (8, (r"\bminerals\b", r"\border"), False),   # not stated at all
    (None, (r"\border",), True),         # not stated is fine
])
def test_weir_grounding(value, near, ok):
    assert oem._grounded(value, WEIR, pct=True, near=near) is ok


def test_level_grounding_whole_number_and_context():
    text = "Order intake in the quarter was DKK 4,643m, against 14,643m for the year."
    assert oem._grounded(4643, text, near=(r"order intake",))
    assert not oem._grounded(4644, text, near=(r"order intake",))
    assert not oem._grounded(643, text, near=(r"order intake",))   # fragment
    far = "Order intake rose. " + "x" * 500 + " Revenue was DKK 4,643m."
    assert not oem._grounded(4643, far, near=(r"order intake",))


def test_fls_validation_period_basis_range():
    text = "Mining order intake for the quarter was DKK 4,643m."
    doc = {"period_end": pd.Timestamp("2016-03-31")}
    ok = {"period_end": "2016-03-31", "order_intake_dkkm": 4643, "basis": "mining"}
    assert oem._validate_fls(ok, text, doc)[1] == 4643
    with pytest.raises(oem.ExtractionRejected, match="quarter"):
        oem._validate_fls({**ok, "period_end": "2015-12-31"}, text, doc)
    with pytest.raises(oem.ExtractionRejected, match="basis"):
        oem._validate_fls({**ok, "basis": "group"}, text, doc)
    with pytest.raises(oem.ExtractionRejected, match="implausible"):
        oem._validate_fls({**ok, "order_intake_dkkm": 443},
                          "Mining order intake DKK 443m", doc)


def test_weir_url_filter_keeps_results_drops_releases():
    base = "https://www.global.weir/newsroom/global-news/"
    keep = ["2026/weir-q1-2026-ims/", "weir-publishes-2018-half-year-results/",
            "2024/Full-Year-Results-2023/", "q3-trading-update-and-cybersecurity-incident/",
            "2015/interim-management-statement2/",
            "the-weir-group-plc-reports-its-financial-results-for-the-full-year-ended-31-december-2019/"]
    drop = ["2026/micromines-2026-half-year-release-improves-how-mining-teams-connect/",
            "weir-minerals-total-asset-management-plan-increases-productivity-and-"
            "results-in-significant-savings-for-tronox/"]
    assert all(oem.WEIR_RESULTS_URL.search(base + u) for u in keep)
    assert not any(oem.WEIR_RESULTS_URL.search(base + u) for u in drop)


def test_tungsten_counts_distinct_completed_months():
    from lbl_tracker.ingest.tungsten_news import monthly_counts
    ret = pd.Timestamp("2026-08-01", tz="UTC")
    flags = pd.DataFrame([
        # same headline matched by two keywords -> one event
        {"title": "China tightens tungsten exports", "published": "2026-09-03 10:00",
         "keyword": "China tungsten", "retrieved_at": ret},
        {"title": "China  tightens tungsten exports", "published": "2026-09-03 11:00",
         "keyword": "tungsten export control", "retrieved_at": ret},
        {"title": "APT price climbs", "published": "2026-09-20 00:00", "keyword": "x",
         "retrieved_at": ret},
        {"title": "Running month item", "published": "2026-11-02 00:00", "keyword": "x",
         "retrieved_at": ret},
    ])
    out = monthly_counts(flags, pd.Timestamp("2026-11-11", tz="UTC"))
    assert out.to_dict() == {pd.Period("2026-09", "M"): 2.0}   # Nov not complete


def test_capex_same_quarter_yoy_needs_both_quarters():
    from lbl_tracker.ingest.abs_capex import same_quarter_yoy
    dates = pd.to_datetime(["2024-03-31", "2024-06-30", "2025-03-31", "2025-06-30",
                            "2025-09-30"])
    exp = pd.DataFrame({"series_id": "abs.capex.mining_expected", "date": dates,
                        "value": [100.0, 200.0, 110.0, 180.0, 50.0],
                        "source_url": "synthetic://", "retrieved_at": pd.Timestamp("2026-01-01")})
    out = same_quarter_yoy(exp).set_index("date")["value"].round(6).to_dict()
    # 2025-09 has no 2024-09 reading -> no point (never interpolated)
    assert out == {pd.Timestamp("2025-03-31"): 10.0, pd.Timestamp("2025-06-30"): -10.0}


def test_cat_missing_months():
    from lbl_tracker.ingest.cat_edgar import missing_months
    prior = pd.DataFrame({"date": pd.to_datetime(["2019-05-31", "2019-06-30",
                                                  "2019-09-30"])})
    assert [str(p) for p in missing_months(prior)] == ["2019-07", "2019-08"]


def _obs(sid, dates, values):
    return pd.DataFrame({"series_id": sid, "date": pd.to_datetime(dates), "value": values,
                         "source_url": "synthetic://", "retrieved_at": pd.Timestamp("2026-01-01", tz="UTC")})


def test_purge_is_audited_and_run_once_runs_once(isolated_root):
    from lbl_tracker.store import (purge_observations, read_series, run_once,
                                   write_observations)
    write_observations("src", _obs("a", ["2020-01-31", "2020-02-29"], [1.0, 2.0]))
    assert purge_observations("src", "a", "test", dates=["2020-01-31"]) == 1
    assert list(read_series("a")["value"]) == [2.0]
    gaps = (isolated_root / "data" / "logs" / "gaps.jsonl").read_text()
    assert "PURGED 1 row(s): test" in gaps and "2020-01-31=1.0" in gaps
    calls = []
    assert run_once("m1", lambda: calls.append(1) or "done") is True
    assert run_once("m1", lambda: calls.append(1)) is False
    assert calls == [1]
    logged = [json.loads(x) for x in
              (isolated_root / "data" / "logs" / "migrations.jsonl").read_text().splitlines()]
    assert logged[0]["id"] == "m1" and logged[0]["result"] == "done"
