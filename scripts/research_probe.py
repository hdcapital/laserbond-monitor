"""One-off research probe round 9: why did the 2026 Weir articles yield
null order-growth figures? Print their tag-stripped text so the
extraction window/prompt can be fixed. Delete when research is done."""
from __future__ import annotations

import re
import sys

URLS = [
    "https://www.global.weir/newsroom/global-news/2026/"
    "weir-reports-its-interim-results-for-the-six-months-ended-30-june-2026/",
    "https://www.global.weir/newsroom/global-news/2026/weir-q1-2026-ims/",
    "https://www.global.weir/newsroom/global-news/2025/weir-half-year-results-2025/",
]


def main() -> int:
    from curl_cffi import requests as creq
    for u in URLS:
        try:
            r = creq.get(u, impersonate="chrome", timeout=60)
            text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", r.text,
                          flags=re.S | re.I)
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"\s+", " ", text)
            print(f"\n===BEGIN {u[-50:]}===")
            print(f"HTTP {r.status_code} textlen={len(text)}")
            hits = re.findall(r"[^.]{0,150}[Oo]rders?[^.]{0,150}\.", text)
            print("ORDER SENTENCES:")
            print("\n---\n".join(hits[:10]) or "(none)")
            print("FIRST 3000:")
            print(text[:3000])
            print(f"===END {u[-50:]}===")
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR {u}: {exc!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
