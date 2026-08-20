"""Backtest harness: pulses vs actual LBL segment outcomes.

Runs ONLY when data/manual/lbl_segment_history.csv has been populated from
actual LBL filings (see data/manual/README.md). Nothing is ever inferred
or filled in for missing halves.

Method:
  * outcome = YoY % change of each segment's half revenue vs the same half
    a year earlier (same-half comparison neutralises LBL's 2H skew);
  * predictor = mean pulse level over the 6 months ending L months before
    the half end, for L in config backtest.lags_months;
  * Spearman rank correlation per (pulse, segment, lag);
  * leave-one-out ridge regression (single standardised predictor,
    lambda=1.0) benchmarked against the naive seasonal base case, which
    predicts each half's YoY growth as the mean of that segment's previous
    same-half YoY growths (pure seasonality, no external data).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd

from ..config import REPO_ROOT, cfg

log = logging.getLogger("lbl_tracker.backtest")

EXPECTED_COLUMNS = [
    "half", "services_rev", "products_rev", "technology_rev",
    "services_margin_pct", "products_margin_pct", "technology_margin_pct",
]
SEGMENTS = {"services": "services_rev", "products": "products_rev",
            "technology": "technology_rev"}


def half_end(half: str) -> pd.Timestamp:
    """'2024H1' -> 2024-06-30, '2024H2' -> 2024-12-31 (calendar halves)."""
    half = str(half).strip().upper()
    year, part = int(half[:4]), half[-2:]
    if part == "H1":
        return pd.Timestamp(year=year, month=6, day=30)
    if part == "H2":
        return pd.Timestamp(year=year, month=12, day=31)
    raise ValueError(f"bad half {half!r}; expected e.g. 2024H1")


def load_history() -> pd.DataFrame | None:
    path = REPO_ROOT / cfg("backtest", "manual_history",
                           default="data/manual/lbl_segment_history.csv")
    if not path.exists():
        log.warning("backtest: %s missing", path)
        return None
    df = pd.read_csv(path)
    if df.empty:
        return None
    missing = [c for c in EXPECTED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"lbl_segment_history.csv missing columns {missing}")
    df["half_end"] = df["half"].map(half_end)
    return df.sort_values("half_end").reset_index(drop=True)


def load_pulses() -> pd.DataFrame:
    path = REPO_ROOT / "data/parquet/pulses.parquet"
    if not path.exists():
        raise FileNotFoundError("run `lbl-tracker pulses` first")
    return pd.read_parquet(path)


def pulse_at_lag(pulses: pd.DataFrame, pulse: str, when: pd.Timestamp,
                 lag_months: int) -> float:
    end = when - pd.DateOffset(months=lag_months)
    start = end - pd.DateOffset(months=6)
    sel = pulses[(pulses["pulse"] == pulse) & (pulses["month"] > start)
                 & (pulses["month"] <= end)]
    vals = sel["value"].dropna()
    return float(vals.mean()) if len(vals) else np.nan


def _loo_ridge_mae(x: np.ndarray, y: np.ndarray, lam: float = 1.0) -> float:
    """Leave-one-out MAE of ridge y ~ a + b*x (x standardised)."""
    errs = []
    n = len(x)
    for i in range(n):
        mask = np.arange(n) != i
        xt, yt = x[mask], y[mask]
        mu, sd = xt.mean(), xt.std() or 1.0
        xs = (xt - mu) / sd
        b = (xs @ (yt - yt.mean())) / (xs @ xs + lam)
        a = yt.mean()
        pred = a + b * (x[i] - mu) / sd
        errs.append(abs(pred - y[i]))
    return float(np.mean(errs))


def run() -> dict:
    history = load_history()
    if history is None:
        msg = ("lbl_segment_history.csv is empty - populate it from actual LBL "
               "filings (see data/manual/README.md) to enable the backtest.")
        log.warning(msg)
        return {"available": False, "note": msg}
    pulses = load_pulses()
    lags = cfg("backtest", "lags_months", default=[0, 3, 6, 9, 12])

    # YoY same-half growth per segment.
    history = history.set_index("half_end")
    results = []
    for pulse_name in pulses["pulse"].unique():
        for segment, col in SEGMENTS.items():
            rev = pd.to_numeric(history[col], errors="coerce")
            yoy = (rev / rev.shift(2) - 1.0) * 100.0  # shift(2) = same half last year
            for lag in lags:
                x = np.array([pulse_at_lag(pulses, pulse_name, ts, lag)
                              for ts in history.index])
                frame = pd.DataFrame({"x": x, "y": yoy.values}).dropna()
                if len(frame) < 6:
                    results.append({"pulse": pulse_name, "segment": segment,
                                    "lag": lag, "n": int(len(frame)),
                                    "note": "insufficient overlapping halves"})
                    continue
                spearman = frame["x"].corr(frame["y"], method="spearman")
                ridge_mae = _loo_ridge_mae(frame["x"].values, frame["y"].values)
                # naive seasonal base: predict mean of previous same-half YoY
                naive_errs = []
                yv = frame["y"].values
                for i in range(2, len(yv)):
                    naive_errs.append(abs(yv[:i].mean() - yv[i]))
                naive_mae = float(np.mean(naive_errs)) if naive_errs else np.nan
                results.append({
                    "pulse": pulse_name, "segment": segment, "lag": lag,
                    "n": int(len(frame)),
                    "spearman": round(float(spearman), 3),
                    "ridge_loo_mae": round(ridge_mae, 2),
                    "naive_seasonal_mae": (round(naive_mae, 2)
                                           if not np.isnan(naive_mae) else None),
                    "beats_naive": (bool(ridge_mae < naive_mae)
                                    if not np.isnan(naive_mae) else None),
                })
    out = {"available": True, "halves": int(len(history)), "results": results}
    path = REPO_ROOT / "docs" / "data" / "backtest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=1))
    return out
