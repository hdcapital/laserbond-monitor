"""Commentary is only ever shown for the figures it was written from."""
import importlib
import json

import pytest


@pytest.fixture()
def comm(tmp_path, monkeypatch):
    monkeypatch.setenv("LBL_REPO_ROOT", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "store: {parquet_dir: data/parquet, logs_dir: data/logs}\n")
    import lbl_tracker.config as config
    importlib.reload(config)
    config.load_config.cache_clear()
    import lbl_tracker.analytics.commentary as commentary
    importlib.reload(commentary)
    data = tmp_path / "docs" / "data"
    data.mkdir(parents=True)
    (data / "pulses.json").write_text(json.dumps({
        "pulses": {"demo": {"title": "Demo", "latest_month": "2027-01-31",
                            "latest_value": None, "weight_available": 0.35,
                            "last_scored_month": "2026-12-31", "history": []}},
        "attribution": {"demo": []}, "technology": {"available": False}}))
    (data / "commentary.json").write_text(json.dumps({
        "demo": {"text": "At 36.7 the pulse is high.", "model": "m",
                 "generated_at": "x", "facts_hash": "outdated"}}))
    yield commentary
    config.load_config.cache_clear()


def _read(comm):
    return json.loads(comm.OUT_PATH.read_text())


def test_outdated_text_removed_without_key(comm, monkeypatch):
    monkeypatch.setattr(comm.llm_client, "have_key", lambda: False)
    comm.generate()
    assert _read(comm) == {}


def test_outdated_text_removed_on_api_failure_or_empty_reply(comm, monkeypatch):
    monkeypatch.setattr(comm.llm_client, "have_key", lambda: True)

    def boom(_):
        raise RuntimeError("api down")
    monkeypatch.setattr(comm.llm_client, "complete", boom)
    comm.generate()
    assert _read(comm) == {}
    monkeypatch.setattr(comm.llm_client, "complete", lambda _: "   ")
    comm.generate()
    assert _read(comm) == {}


def test_regenerated_then_cached(comm, monkeypatch):
    calls = []
    monkeypatch.setattr(comm.llm_client, "have_key", lambda: True)
    monkeypatch.setattr(comm.llm_client, "model_name", lambda: "m")
    monkeypatch.setattr(comm.llm_client, "complete",
                        lambda prompt: calls.append(prompt) or "NO DATA this month.")
    comm.generate()
    assert _read(comm)["demo"]["text"] == "NO DATA this month."
    assert '"latest_value": null' in calls[0]
    assert '"share_of_input_weight_available": 0.35' in calls[0]
    # unchanged figures -> kept, no new call, even if the key later disappears
    monkeypatch.setattr(comm.llm_client, "have_key", lambda: False)
    comm.generate()
    assert len(calls) == 1 and _read(comm)["demo"]["text"] == "NO DATA this month."
