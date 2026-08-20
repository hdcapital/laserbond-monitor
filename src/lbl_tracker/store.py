"""Parquet + DuckDB store.

Every observation row carries: series_id, date, value, source_url,
retrieved_at. Data integrity rules:

* values are only ever what a source actually published; NULL means the
  source presented the period with no value, or a fetch failed for a
  period we already track (gap logged separately in data/logs/gaps.jsonl);
* writes are idempotent: rows are deduplicated on (series_id, date) with
  the newest non-null fetch winning, so revisions flow through but a
  broken fetch can never erase real history.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd

from .config import REPO_ROOT, cfg, data_dir, logs_dir

log = logging.getLogger("lbl_tracker.store")

OBS_COLUMNS = ["series_id", "date", "value", "source_url", "retrieved_at"]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _obs_path(source: str) -> Path:
    path = data_dir() / "observations" / f"{source}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _events_path(name: str) -> Path:
    path = data_dir() / "events" / f"{name}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def normalize_observations(df: pd.DataFrame) -> pd.DataFrame:
    missing = [c for c in OBS_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"observation frame missing columns: {missing}")
    out = df[OBS_COLUMNS].copy()
    out["series_id"] = out["series_id"].astype(str)
    out["date"] = pd.to_datetime(out["date"]).dt.tz_localize(None)
    out["value"] = pd.to_numeric(out["value"], errors="coerce")
    out["source_url"] = out["source_url"].astype(str)
    out["retrieved_at"] = pd.to_datetime(out["retrieved_at"], utc=True)
    return out


def write_observations(source: str, df: pd.DataFrame) -> dict:
    """Idempotent upsert of observation rows for one source file."""
    new = normalize_observations(df)
    path = _obs_path(source)
    if path.exists():
        old = pd.read_parquet(path)
        combined = pd.concat([old, new], ignore_index=True)
    else:
        combined = new
    # newest non-null fetch wins; a null can only stand if the key has no
    # non-null row at all
    combined["_has_value"] = combined["value"].notna().astype(int)
    combined = combined.sort_values(["series_id", "date", "_has_value", "retrieved_at"])
    combined = combined.drop_duplicates(["series_id", "date"], keep="last")
    combined = combined.drop(columns="_has_value").sort_values(["series_id", "date"])
    combined.to_parquet(path, index=False)
    stats = {
        "source": source,
        "rows_written": int(len(new)),
        "rows_total": int(len(combined)),
        "series": sorted(new["series_id"].unique().tolist()),
    }
    log.info("store %s: +%d rows (total %d)", source, stats["rows_written"], stats["rows_total"])
    return stats


def read_observations(sources: list[str] | None = None) -> pd.DataFrame:
    obs_dir = data_dir() / "observations"
    frames = []
    if obs_dir.exists():
        for path in sorted(obs_dir.glob("*.parquet")):
            if sources and path.stem not in sources:
                continue
            frames.append(pd.read_parquet(path))
    if not frames:
        return pd.DataFrame(columns=OBS_COLUMNS)
    return pd.concat(frames, ignore_index=True)


def read_series(series_id: str) -> pd.DataFrame:
    df = read_observations()
    return df[df["series_id"] == series_id].sort_values("date").reset_index(drop=True)


def write_events(name: str, df: pd.DataFrame, key: str) -> dict:
    """Idempotent upsert of event rows (announcements, news flags, ...)."""
    if key not in df.columns:
        raise ValueError(f"event frame missing key column {key!r}")
    path = _events_path(name)
    if path.exists():
        old = pd.read_parquet(path)
        combined = pd.concat([old, df], ignore_index=True)
    else:
        combined = df.copy()
    if "retrieved_at" in combined.columns:
        combined = combined.sort_values("retrieved_at")
    combined = combined.drop_duplicates(key, keep="last").reset_index(drop=True)
    combined.to_parquet(path, index=False)
    return {"events": name, "rows_written": int(len(df)), "rows_total": int(len(combined))}


def read_events(name: str) -> pd.DataFrame:
    path = _events_path(name)
    if not path.exists():
        return pd.DataFrame()
    return pd.read_parquet(path)


def stable_id(*parts: str) -> str:
    return hashlib.sha1("||".join(str(p) for p in parts).encode()).hexdigest()[:16]


# --- gap + run logging ------------------------------------------------------

def log_gap(source: str, series_id: str, detail: str) -> None:
    entry = {
        "ts": now_utc().isoformat(),
        "source": source,
        "series_id": series_id,
        "detail": detail,
    }
    with open(logs_dir() / "gaps.jsonl", "a") as fh:
        fh.write(json.dumps(entry) + "\n")
    log.warning("GAP %s/%s: %s", source, series_id, detail)


def log_run(source: str, status: str, rows: int = 0, error: str = "") -> None:
    entry = {
        "ts": now_utc().isoformat(),
        "source": source,
        "status": status,
        "rows": rows,
        "error": error[:2000],
    }
    with open(logs_dir() / "runs.jsonl", "a") as fh:
        fh.write(json.dumps(entry) + "\n")


def build_duckdb() -> Path:
    """(Re)build the DuckDB database as views over the parquet store."""
    db_path = REPO_ROOT / cfg("store", "duckdb_path", default="data/lbl.duckdb")
    db_path.unlink(missing_ok=True)
    con = duckdb.connect(str(db_path))
    obs_glob = str(data_dir() / "observations" / "*.parquet")
    if list((data_dir() / "observations").glob("*.parquet")):
        con.execute(f"CREATE VIEW observations AS SELECT * FROM read_parquet('{obs_glob}')")
    for path in sorted((data_dir() / "events").glob("*.parquet")):
        con.execute(f"CREATE VIEW {path.stem} AS SELECT * FROM read_parquet('{path}')")
    con.close()
    return db_path
