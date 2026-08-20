"""Unit tests. Any numbers below are synthetic fixtures exercising parsers
and guards - they never enter the data store or dashboards."""
import pandas as pd
import pytest


def test_backtest_refuses_empty_history(monkeypatch, tmp_path):
    monkeypatch.setenv("LBL_REPO_ROOT", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "store: {parquet_dir: data/parquet, logs_dir: data/logs}\n"
        "backtest: {manual_history: data/manual/lbl_segment_history.csv,"
        " lags_months: [0, 3]}\n")
    manual = tmp_path / "data" / "manual"
    manual.mkdir(parents=True)
    (manual / "lbl_segment_history.csv").write_text(
        "half,services_rev,products_rev,technology_rev,"
        "services_margin_pct,products_margin_pct,technology_margin_pct\n")
    import importlib

    import lbl_tracker.config as config
    importlib.reload(config)
    config.load_config.cache_clear()
    import lbl_tracker.analytics.backtest as backtest
    importlib.reload(backtest)
    out = backtest.run()
    assert out["available"] is False


def test_half_end_parsing():
    from lbl_tracker.analytics.backtest import half_end
    assert half_end("2024H1") == pd.Timestamp("2024-06-30")
    assert half_end("2024h2") == pd.Timestamp("2024-12-31")
    with pytest.raises(ValueError):
        half_end("2024Q1")


def test_repo_ships_no_populated_manual_history():
    """Integrity rule: the manual LBL history must stay empty in the repo."""
    from pathlib import Path
    path = Path(__file__).resolve().parents[2] / "data/manual/lbl_segment_history.csv"
    df = pd.read_csv(path)
    assert df.empty, "lbl_segment_history.csv must only be populated locally from filings"


def test_rba_csv_parser_shape():
    from lbl_tracker.ingest.rba import parse_rba_csv
    csv = (
        "F11.1 Exchange Rates\n"
        "Title,US dollar,Trade-weighted index\n"
        "Frequency,Monthly,Monthly\n"
        "Units,USD,Index\n"
        "Series ID,FXRUSD,FXRTWI\n"
        "01-Jun-2025,0.6501,60.1\n"
        "01-Jul-2025,0.6612,60.9\n"
        "01-Aug-2025,,61.2\n"
    )
    data, meta = parse_rba_csv(csv)
    assert meta["FXRUSD"] == "US dollar"
    assert len(data) == 3
    assert data["FXRUSD"].iloc[1] == pytest.approx(0.6612)
    assert pd.isna(data["FXRUSD"].iloc[2])  # blank stays NULL


def test_importgenius_dormant(monkeypatch):
    monkeypatch.delenv("IMPORTGENIUS_API_KEY", raising=False)
    from lbl_tracker.ingest.importgenius import ingest
    out = ingest()
    assert out == {"rows_written": 0, "dormant": True}
