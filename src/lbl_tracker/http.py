"""Shared HTTP session with retry/backoff and request logging."""
from __future__ import annotations

import logging
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import user_agent

log = logging.getLogger("lbl_tracker.http")

DEFAULT_TIMEOUT = 60


class SourceFetchError(RuntimeError):
    """A source endpoint failed or returned something unusable."""


def make_session(sec: bool = False, extra_headers: dict | None = None) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "HEAD"),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers["User-Agent"] = user_agent(sec=sec)
    if extra_headers:
        session.headers.update(extra_headers)
    return session


def get(url: str, *, session: requests.Session | None = None, sec: bool = False,
        timeout: int = DEFAULT_TIMEOUT, **kwargs) -> requests.Response:
    session = session or make_session(sec=sec)
    started = time.monotonic()
    resp = session.get(url, timeout=timeout, **kwargs)
    log.info("GET %s -> %s (%.1fs, %d bytes)", url, resp.status_code,
             time.monotonic() - started, len(resp.content or b""))
    if resp.status_code != 200:
        raise SourceFetchError(f"GET {url} -> HTTP {resp.status_code}: {resp.text[:300]}")
    return resp
