import pytest


@pytest.fixture()
def isolated_root(tmp_path, monkeypatch):
    """Point the store/log root at a temp dir so a test can never write to
    the real data/ tree (config.REPO_ROOT is read at call time by data_dir/
    logs_dir; monkeypatch restores it afterwards)."""
    import lbl_tracker.config as config
    (tmp_path / "config.yaml").write_text(
        "store: {parquet_dir: data/parquet, logs_dir: data/logs}\n")
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    config.load_config.cache_clear()
    yield tmp_path
    config.load_config.cache_clear()
