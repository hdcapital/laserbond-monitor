"""Monthly pulse composites (-100..+100) from stored observations.

Method (documented, deterministic, no data invention):
  1. Each component series is reduced to a monthly series using only the
     months in which the source actually published (mean of observations
     within the month; quarterly values sit in their quarter-end month).
  2. Each monthly point is z-scored against a 5-year trailing window of
     that same series (config zscore.window_months), requiring
     zscore.min_history_months of prior observations; earlier points get
     no score (NO DATA, never an invented baseline).
  3. z is clipped at +/- zscore.clip_sigma and scaled to [-100, 100].
  4. A pulse month combines each component's most recent scored month
     within its max_stale_months (frequency alignment, not backfill:
     the underlying store is untouched). Weights renormalise over the
     components that are present; if less than half the configured weight
     is present, the pulse is NULL for that month.

Technology Pipeline is intentionally not a composite: it is an event
table summarised as facts.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd

from ..config import REPO_ROOT, cfg
from ..store import read_events, read_observations

log = logging.getLogger("lbl_tracker.pulses")

PULSES_PARQUET = "data/parquet/pulses.parquet"


def monthly_series(obs: pd.DataFrame, series_id: str) -> pd.Series:
    sel = obs[(obs["series_id"] == series_id) & obs["value"].notna()]
    if sel.empty:
        return pd.Series(dtype=float)
    monthly = (sel.assign(month=pd.to_datetime(sel["date"]).dt.to_period("M"))
               .groupby("month")["value"].mean())
    monthly.index = monthly.index.to_timestamp("M")
    return monthly.sort_index()


def zscore(series: pd.Series) -> pd.DataFrame:
    """Rolling z per observed month. Columns: value, z, score."""
    window = int(cfg("zscore", "window_months", default=60))
    min_hist = int(cfg("zscore", "min_history_months", default=24))
    clip = float(cfg("zscore", "clip_sigma", default=2.5))
    if series.empty:
        return pd.DataFrame(columns=["value", "z", "score"],
                            index=pd.DatetimeIndex([]))
    roll = series.rolling(window=pd.Timedelta(days=int(window * 30.44)),
                          min_periods=min_hist)
    mean, std = roll.mean(), roll.std()
    z = (series - mean) / std.replace(0.0, np.nan)
    score = z.clip(-clip, clip) / clip * 100.0
    return pd.DataFrame({"value": series, "z": z, "score": score})


def compute_pulse(name: str, obs: pd.DataFrame) -> tuple[pd.DataFrame, list[dict]]:
    """Return (monthly pulse frame, latest-month attribution)."""
    spec = cfg("pulses", name)
    if not spec:
        raise KeyError(f"pulse {name!r} not in config")
    components = spec["components"]

    scored = {}
    for comp in components:
        scored[comp["series"]] = zscore(monthly_series(obs, comp["series"]))

    # Month range: from the earliest scored month to the latest.
    all_months = sorted({m for df in scored.values() for m in df.index[df["score"].notna()]})
    if not all_months:
        empty = pd.DataFrame(columns=["month", "pulse", "value", "weight_available"])
        return empty, [{"series": c["series"], "label": c.get("label", c["series"]),
                        "status": "NO DATA"} for c in components]

    months = pd.period_range(all_months[0], all_months[-1], freq="M").to_timestamp("M")
    total_weight = sum(c["weight"] for c in components)
    rows, attribution = [], []
    for month in months:
        parts = []
        for comp in components:
            df = scored[comp["series"]]
            valid = df[(df.index <= month) & df["score"].notna()]
            if valid.empty:
                continue
            last = valid.iloc[-1]
            stale = (month.to_period("M") - valid.index[-1].to_period("M")).n
            if stale > comp["max_stale_months"]:
                continue
            sign = -1.0 if comp.get("invert") else 1.0
            parts.append({
                "series": comp["series"], "label": comp.get("label", comp["series"]),
                "weight": comp["weight"], "score": float(last["score"]) * sign,
                "raw_score": float(last["score"]), "z": float(last["z"]),
                "value": float(last["value"]), "stale_months": int(stale),
                "as_of": valid.index[-1].date().isoformat(),
                "inverted": bool(comp.get("invert", False)),
            })
        weight_avail = sum(p["weight"] for p in parts)
        if weight_avail >= 0.5 * total_weight:
            value = sum(p["score"] * p["weight"] for p in parts) / weight_avail
        else:
            value = np.nan
        rows.append({"month": month, "pulse": name, "value": value,
                     "weight_available": weight_avail / total_weight})
        if month == months[-1]:
            for comp in components:
                part = next((p for p in parts if p["series"] == comp["series"]), None)
                if part:
                    part = dict(part)
                    part["weight_used"] = part["weight"] / weight_avail if weight_avail else 0
                    part["contribution"] = (part["score"] * part["weight"] / weight_avail
                                            if weight_avail else None)
                    attribution.append(part)
                else:
                    attribution.append({"series": comp["series"],
                                        "label": comp.get("label", comp["series"]),
                                        "weight": comp["weight"], "status": "NO DATA"})
    return pd.DataFrame(rows), attribution


def technology_pipeline() -> dict:
    """Facts from the LBL Technology event table - never scored."""
    events = read_events("lbl_technology_events")
    stages = ["lead", "trial", "agreement", "cell_ordered", "commissioned", "recurring"]
    if events.empty:
        return {"available": False, "stages": stages, "events": [],
                "note": "No classified Technology events yet (needs announcement "
                        "ingest + ANTHROPIC_API_KEY)."}
    events = events.copy()
    events["date"] = pd.to_datetime(events["date"])
    now = events["date"].max()
    six_months_ago = now - pd.DateOffset(months=6)

    def stage_counts(cutoff) -> dict:
        sel = events[events["date"] <= cutoff]
        return {s: int((sel["stage"] == s).sum()) for s in stages}

    counts_now = stage_counts(now)
    counts_prior = stage_counts(six_months_ago)
    contracted_stages = ["agreement", "cell_ordered", "commissioned", "recurring"]
    contracted = events[events["stage"].isin(contracted_stages)]
    contracted_total = float(pd.to_numeric(contracted["value_aud"],
                                           errors="coerce").sum())
    recognised_total = float(pd.to_numeric(contracted["recognised_aud"],
                                           errors="coerce").sum())
    return {
        "available": True,
        "stages": stages,
        "stage_counts": counts_now,
        "stage_counts_6m_ago": counts_prior,
        "stage_deltas_6m": {s: counts_now[s] - counts_prior[s] for s in stages},
        "contracted_value_aud_where_stated": contracted_total,
        "recognised_aud_where_stated": recognised_total,
        "contracted_unrecognised_aud_where_stated": contracted_total - recognised_total,
        "events": events.sort_values("date", ascending=False)
                        .head(50)
                        .assign(date=lambda d: d["date"].dt.date.astype(str))
                        .to_dict("records"),
    }


def compute_all(write: bool = True) -> dict:
    obs = read_observations()
    result = {"pulses": {}, "attribution": {}, "technology": technology_pipeline()}
    frames = []
    for name in cfg("pulses", default={}):
        frame, attribution = compute_pulse(name, obs)
        frames.append(frame)
        result["attribution"][name] = attribution
        if len(frame):
            latest = frame.dropna(subset=["value"]).tail(1)
            result["pulses"][name] = {
                "title": cfg("pulses", name, "title", default=name),
                "latest_month": (latest["month"].iloc[0].date().isoformat()
                                 if len(latest) else None),
                "latest_value": (round(float(latest["value"].iloc[0]), 1)
                                 if len(latest) else None),
                "history": [
                    {"month": r["month"].date().isoformat(),
                     "value": (None if pd.isna(r["value"]) else round(float(r["value"]), 2))}
                    for r in frame.to_dict("records")],
            }
        else:
            result["pulses"][name] = {
                "title": cfg("pulses", name, "title", default=name),
                "latest_month": None, "latest_value": None, "history": [],
            }
    if write and frames:
        out = pd.concat(frames, ignore_index=True)
        path = REPO_ROOT / PULSES_PARQUET
        path.parent.mkdir(parents=True, exist_ok=True)
        out.to_parquet(path, index=False)
    if write:
        json_path = REPO_ROOT / "docs" / "data" / "pulses.json"
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(result, indent=1, default=str))
    return result
