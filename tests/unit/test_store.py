"""Store semantics: idempotency, revision handling, NULL protection.

Values in these tests are arbitrary synthetic integers exercising the
upsert logic - they never enter the real data store (tmp_path).
"""
import pandas as pd
import pytest


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("LBL_REPO_ROOT", str(tmp_path))
    import importlib

    import lbl_tracker.config as config
    import lbl_tracker.store as store_mod
    importlib.reload(config)
    (tmp_path / "config.yaml").write_text(
        "store:\n  parquet_dir: data/parquet\n  logs_dir: data/logs\n")
    config.load_config.cache_clear()
    importlib.reload(store_mod)
    return store_mod


def _frame(store, values, retrieved="2026-01-01T00:00:00Z"):
    return pd.DataFrame({
        "series_id": "t.series",
        "date": pd.to_datetime([d for d, _ in values]),
        "value": [v for _, v in values],
        "source_url": "https://example.test/data",
        "retrieved_at": pd.Timestamp(retrieved),
    })


def test_idempotent_rewrite(store):
    df = _frame(store, [("2025-01-31", 1.0), ("2025-02-28", 2.0)])
    store.write_observations("t", df)
    store.write_observations("t", df)
    out = store.read_observations()
    assert len(out) == 2


def test_revision_newest_wins(store):
    store.write_observations("t", _frame(store, [("2025-01-31", 1.0)], "2026-01-01T00:00:00Z"))
    store.write_observations("t", _frame(store, [("2025-01-31", 5.0)], "2026-02-01T00:00:00Z"))
    out = store.read_observations()
    assert len(out) == 1 and out["value"].iloc[0] == 5.0


def test_null_never_erases_value(store):
    store.write_observations("t", _frame(store, [("2025-01-31", 1.0)], "2026-01-01T00:00:00Z"))
    store.write_observations("t", _frame(store, [("2025-01-31", None)], "2026-02-01T00:00:00Z"))
    out = store.read_observations()
    assert len(out) == 1 and out["value"].iloc[0] == 1.0


def test_null_kept_when_no_value_exists(store):
    store.write_observations("t", _frame(store, [("2025-03-31", None)]))
    out = store.read_observations()
    assert len(out) == 1 and pd.isna(out["value"].iloc[0])


def test_missing_column_rejected(store):
    df = pd.DataFrame({"series_id": ["x"], "date": ["2025-01-31"], "value": [1.0]})
    with pytest.raises(ValueError):
        store.write_observations("t", df)


def test_gap_logged(store):
    store.log_gap("src", "series.x", "endpoint down")
    log = (store.logs_dir() / "gaps.jsonl").read_text()
    assert "endpoint down" in log and "series.x" in log
