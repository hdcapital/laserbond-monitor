"""Baker Hughes block detection - synthetic, no network."""
import pytest

from lbl_tracker.http import SourceBlockedError, SourceFetchError
from lbl_tracker.ingest import baker_hughes


def _fake_get(statuses):
    def fake(url, session=None, **_):
        raise SourceFetchError(f"GET {url} -> HTTP {statuses[url]}: Access Denied",
                               status=statuses[url])
    return fake


def test_all_pages_403_raises_blocked(monkeypatch):
    monkeypatch.setattr(baker_hughes, "get",
                        _fake_get({p: 403 for p in baker_hughes.PAGES}))
    with pytest.raises(SourceBlockedError) as exc:
        baker_hughes.fetch()
    assert str(exc.value).startswith("BLOCKED")
    assert exc.value.status == 403


def test_mixed_failures_are_not_blocked(monkeypatch):
    statuses = {p: 403 for p in baker_hughes.PAGES}
    statuses[baker_hughes.PAGES[0]] = 500
    monkeypatch.setattr(baker_hughes, "get", _fake_get(statuses))
    with pytest.raises(RuntimeError) as exc:
        baker_hughes.fetch()
    assert not isinstance(exc.value, SourceBlockedError)
    assert "page_errors" in str(exc.value)
