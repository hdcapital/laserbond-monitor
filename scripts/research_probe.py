"""One-off research probe round 10: where does the Weir article body live?
The rendered pages are stubs to non-JS clients; the content may sit in a
JSON hydration blob inside <script> (which tag-stripping removed) or be
fetched from a CMS API. Dump raw-HTML evidence. Delete when done."""
from __future__ import annotations

import re
import sys

URL = ("https://www.global.weir/newsroom/global-news/2025/"
       "weir-half-year-results-2025/")


def main() -> int:
    from curl_cffi import requests as creq
    r = creq.get(URL, impersonate="chrome", timeout=60)
    html = r.text
    print(f"HTTP {r.status_code} rawlen={len(html)}")
    print("count 'order':", len(re.findall(r"order", html, re.I)))
    print("count 'Minerals':", len(re.findall(r"Minerals", html)))
    # contexts where 'orders' appears in the RAW html
    for m in list(re.finditer(r"[Oo]rders", html))[:8]:
        print("CTX:", html[max(0, m.start() - 160):m.start() + 160]
              .replace("\n", " ")[:340])
    # script/json/api hints
    for pat in (r'src="([^"]+\.js[^"]*)"', r'"(https?://[^"]*api[^"]*)"',
                r'href="([^"]+\.pdf[^"]*)"'):
        hits = sorted(set(re.findall(pat, html)))[:12]
        print(f"\nPATTERN {pat}:")
        print("\n".join(hits))
    return 0


if __name__ == "__main__":
    sys.exit(main())
