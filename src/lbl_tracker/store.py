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
    # a parser drifting into fabricated-looking output must fail loudly:
    # nothing we ingest is dated more than ~13 months ahead (ABS short-term
    # capex expectations are the furthest forward-dated real data)
    horizon = pd.Timestamp.now() + pd.Timedelta(days=400)
    bad = out["date"] > horizon
    if bad.any():
        raise ValueError(f"observation dates beyond {horizon.date()} - parser "
                         f"drift?\n{out[bad].head()}")
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


def purge_observations(source: str, series_id: str, reason: str,
                       dates: list | None = None) -> int:
    """Delete stored rows that failed validation - the ONLY way a value
    leaves the store (an upsert can never replace a value with NULL).

    Every purge is audited in gaps.jsonl with the reason, count and dates,
    so a removed number is always traceable. `dates=None` purges the whole
    series. Returns the number of rows removed."""
    path = _obs_path(source)
    if not path.exists():
        return 0
    old = pd.read_parquet(path)
    mask = old["series_id"] == series_id
    if dates is not None:
        wanted = set(pd.to_datetime(pd.Series(list(dates))).dt.normalize())
        mask &= pd.to_datetime(old["date"]).dt.normalize().isin(wanted)
    removed = old[mask]
    if removed.empty:
        return 0
    old[~mask].to_parquet(path, index=False)
    shown = ", ".join(f"{pd.Timestamp(d).date()}={v}" for d, v in
                      zip(removed["date"], removed["value"]))
    log_gap(source, series_id, f"PURGED {len(removed)} row(s): {reason} [{shown[:1500]}]")
    return int(len(removed))


def run_once(migration_id: str, fn) -> bool:
    """Apply a one-off data correction exactly once per store.

    Corrections run inside the scheduled CI ingest (the store is committed
    by CI, so editing parquet files in a PR would race the bot's commits).
    Applied ids are recorded in data/logs/migrations.jsonl; returns True if
    `fn` ran now."""
    path = logs_dir() / "migrations.jsonl"
    if path.exists():
        with open(path) as fh:
            done = {json.loads(line).get("id") for line in fh if line.strip()}
        if migration_id in done:
            return False
    result = fn()
    with open(path, "a") as fh:
        fh.write(json.dumps({"id": migration_id, "ts": now_utc().isoformat(),
                             "result": result}, default=str) + "\n")
    log.info("migration %s applied: %s", migration_id, result)
    return True


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


def blocked_sources() -> dict[str, dict]:
    """Sources whose most recent run failed with an infrastructure block
    (error text starting "BLOCKED", see http.SourceBlockedError).

    Returns {source: {"since": ts of the first failed run after the last
    success, "last_ok": ts of the last successful run or None, "detail":
    first line of the error, "series": [series_id, ...] stored for it}}.
    Derived from data/logs/runs.jsonl, so it clears itself on the first
    successful run - nothing to maintain by hand."""
    path = logs_dir() / "runs.jsonl"
    if not path.exists():
        return {}
    runs: dict[str, list[dict]] = {}
    with open(path) as fh:
        for line in fh:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            runs.setdefault(entry.get("source", ""), []).append(entry)
    out = {}
    for source, entries in runs.items():
        entries.sort(key=lambda e: e.get("ts", ""))
        latest = entries[-1]
        if latest.get("status") != "error" or \
                not str(latest.get("error", "")).startswith("BLOCKED"):
            continue
        # "since" = first failure after the last successful run (earlier
        # failures may predate the BLOCKED label but share the cause).
        since, last_ok = latest["ts"], None
        for entry in reversed(entries):
            if entry.get("status") == "ok":
                last_ok = entry["ts"]
                break
            since = entry["ts"]
        obs = read_observations([source])
        out[source] = {
            "since": since,
            "last_ok": last_ok,
            "detail": str(latest["error"]).split("\n", 1)[0],
            "series": sorted(obs["series_id"].unique()) if len(obs) else [],
        }
    return out


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
