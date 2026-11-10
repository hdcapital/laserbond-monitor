"""Baker Hughes block detection + CI result classification - synthetic, no network."""
import json

import pytest

from lbl_tracker.http import SourceBlockedError, SourceFetchError
from lbl_tracker.ingest import baker_hughes, classify_results


def _fake_get(statuses):
    def fake(url, session=None, **_):
        raise SourceFetchError(f"GET {url} -> HTTP {statuses[url]}: Access Denied",
                               status=statuses[url])
    return fake


def test_all_pages_403_raises_blocked(isolated_root, monkeypatch):
    monkeypatch.setattr(baker_hughes, "get",
                        _fake_get({p: 403 for p in baker_hughes.PAGES}))
    with pytest.raises(SourceBlockedError) as exc:
        baker_hughes.fetch()
    assert str(exc.value).startswith("BLOCKED")
    assert exc.value.status == 403


def test_mixed_failures_are_not_blocked(isolated_root, monkeypatch):
    statuses = {p: 403 for p in baker_hughes.PAGES}
    statuses[baker_hughes.PAGES[0]] = 500
    monkeypatch.setattr(baker_hughes, "get", _fake_get(statuses))
    with pytest.raises(RuntimeError) as exc:
        baker_hughes.fetch()
    assert not isinstance(exc.value, SourceBlockedError)
    assert "page_errors" in str(exc.value)
    # gaps went to the isolated root, not the real store
    assert (isolated_root / "data" / "logs" / "gaps.jsonl").exists()


def test_classify_results_only_known_blocks_are_warnings():
    results = [
        {"source": "rba", "status": "ok"},
        {"source": "baker_hughes", "status": "error", "error": "BLOCKED: 403 on all pages"},
        {"source": "cat_edgar", "status": "error", "error": "HTTP 403 Undeclared Automated Tool"},
        {"source": "aisi", "status": "error", "error": "aisi: no release parsed"},
        # a 403 that is NOT the all-pages block must still fail
        {"source": "jsa_ivi", "status": "error", "error": "GET x -> HTTP 403: denied"},
    ]
    split = classify_results(results)
    assert [r["source"] for r in split["blocked"]] == ["baker_hughes", "cat_edgar"]
    assert [r["source"] for r in split["failed"]] == ["aisi", "jsa_ivi"]


def test_check_results_cli_exit_codes(tmp_path, capsys):
    from lbl_tracker.cli import main
    blocked_only = tmp_path / "a.json"
    blocked_only.write_text(json.dumps([
        {"source": "baker_hughes", "status": "error", "error": "BLOCKED: x\ntrace"}]))
    assert main(["check-results", str(blocked_only)]) == 0
    assert "::warning::baker_hughes BLOCKED: x" in capsys.readouterr().out
    real_fail = tmp_path / "b.json"
    real_fail.write_text(json.dumps([
        {"source": "aisi", "status": "error", "error": "drift"}]))
    assert main(["check-results", str(real_fail)]) == 1
    assert "::error::" in capsys.readouterr().out


def _run(ts, status, error=""):
    return json.dumps({"ts": ts, "source": "baker_hughes", "status": status,
                       "rows": 0, "error": error}) + "\n"


def test_blocked_sources_from_run_log(isolated_root):
    from lbl_tracker.store import blocked_sources
    logs = isolated_root / "data" / "logs"
    logs.mkdir(parents=True)
    (logs / "runs.jsonl").write_text(
        _run("2026-10-06T01:00:00+00:00", "ok")
        + _run("2026-10-13T01:00:00+00:00", "error", "no workbook parsed")
        + _run("2026-11-17T01:00:00+00:00", "error", "BLOCKED: 403 everywhere\ntb"))
    info = blocked_sources()["baker_hughes"]
    assert info["since"].startswith("2026-10-13")   # first failure after last ok
    assert info["last_ok"].startswith("2026-10-06")
    assert info["detail"] == "BLOCKED: 403 everywhere"
    # a later success clears it
    with open(logs / "runs.jsonl", "a") as fh:
        fh.write(_run("2026-11-24T01:00:00+00:00", "ok"))
    assert blocked_sources() == {}
