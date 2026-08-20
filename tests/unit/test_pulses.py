"""Pulse math on synthetic in-memory series (never touches the real store).

Series are generated programmatically (linspace/constant) purely to
exercise the z-score/compositing code paths.
"""
import numpy as np
import pandas as pd
import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("LBL_REPO_ROOT", str(tmp_path))
    (tmp_path / "config.yaml").write_text("""
store: {parquet_dir: data/parquet, logs_dir: data/logs}
zscore: {window_months: 60, min_history_months: 24, clip_sigma: 2.5}
pulses:
  demo:
    title: Demo
    components:
      - {series: syn.up, weight: 0.5, invert: false, max_stale_months: 3, label: Up}
      - {series: syn.flat, weight: 0.5, invert: true, max_stale_months: 3, label: Flat}
""")
    import importlib

    import lbl_tracker.config as config
    importlib.reload(config)
    config.load_config.cache_clear()
    import lbl_tracker.analytics.pulses as pulses
    importlib.reload(pulses)
    return pulses


def _obs(series_id, dates, values):
    return pd.DataFrame({
        "series_id": series_id, "date": pd.to_datetime(dates), "value": values,
        "source_url": "synthetic://unit-test", "retrieved_at": pd.Timestamp("2026-01-01", tz="UTC"),
    })


def test_zscore_needs_min_history(env):
    dates = pd.date_range("2020-01-31", periods=10, freq="ME")
    series = pd.Series(np.linspace(1, 10, 10), index=dates)
    out = env.zscore(series)
    assert out["score"].notna().sum() == 0  # < 24 months -> no invented baseline


def test_zscore_scales_and_clips(env):
    dates = pd.date_range("2018-01-31", periods=80, freq="ME")
    values = np.concatenate([np.ones(79), [100.0]])  # huge final spike
    out = env.zscore(pd.Series(values, index=dates))
    assert out["score"].iloc[-1] == pytest.approx(100.0)  # clipped at +2.5 sigma


def test_composite_weights_and_inversion(env):
    months = pd.date_range("2018-01-31", periods=84, freq="ME")
    rng_up = np.linspace(0, 100, len(months)) + np.tile([0, 3, -3, 1], 21)
    obs = pd.concat([
        _obs("syn.up", months, rng_up),
        _obs("syn.flat", months, np.tile([5.0, 6.0, 4.0], 28)),
    ])
    frame, attribution = env.compute_pulse("demo", obs)
    assert len(frame)
    latest = frame.dropna(subset=["value"]).iloc[-1]
    assert -100 <= latest["value"] <= 100
    labels = {a["label"] for a in attribution}
    assert labels == {"Up", "Flat"}
    flat = next(a for a in attribution if a["label"] == "Flat")
    assert flat["inverted"] is True
    assert flat["score"] == pytest.approx(-flat["raw_score"])


def test_missing_component_renormalises(env):
    months = pd.date_range("2018-01-31", periods=84, freq="ME")
    obs = _obs("syn.up", months, np.linspace(0, 100, len(months)) +
               np.tile([0, 2, -2, 1], 21))
    frame, attribution = env.compute_pulse("demo", obs)
    # one of two equal weights available -> exactly half the weight -> still scored
    latest = frame.iloc[-1]
    assert latest["weight_available"] == pytest.approx(0.5)
    assert not pd.isna(latest["value"])
    statuses = {a.get("status") for a in attribution}
    assert "NO DATA" in statuses


def test_no_data_pulse(env):
    frame, attribution = env.compute_pulse("demo", _obs("other", ["2025-01-31"], [1.0]))
    assert frame.empty
    assert all(a.get("status") == "NO DATA" for a in attribution)
