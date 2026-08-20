"""One-off research probe round 7 (runs in CI where the network is open).

  1. FLSmidth financial-downloads - per-PDF row titles (wide, tag-stripped
     context) so interim reports can be mapped to quarters.
  2. Weir sitemap.xml - parse <loc> URLs (XML, not href) for the results/
     reports archive; also try common results-centre paths.
  3. Investegate (free RNS archive) - Weir announcement list.

Prints machine-parseable blocks to stdout; nothing is written to the store.
Delete this script once the research round is done.
"""
from __future__ import annotations

import re
import sys


def block(name: str, text: str) -> None:
    print(f"\n===BEGIN {name}===")
    print(text)
    print(f"===END {name}===")


def creq_get(url: str):
    from curl_cffi import requests as creq
    return creq.get(url, impersonate="chrome", timeout=60)


def fls_titles() -> None:
    try:
        r = creq_get("https://www.flsmidth.com/en/investors/financial-downloads")
        html = r.text
        out = []
        for m in re.finditer(r'href="(https?://[^"]+\.pdf)"', html):
            start = max(0, m.start() - 1200)
            ctx = re.sub(r"<[^>]+>", "|", html[start:m.start()])
            ctx = re.sub(r"[|\s]+", " ", ctx).strip()
            out.append(f"{m.group(1)} ||| {ctx[-220:]}")
        block("FLS TITLES", f"HTTP {r.status_code} n={len(out)}\n" + "\n".join(out))
    except Exception as exc:  # noqa: BLE001
        block("FLS TITLES ERROR", repr(exc))


def weir_sitemap() -> None:
    try:
        r = creq_get("https://www.global.weir/sitemap.xml")
        locs = re.findall(r"<loc>([^<]+)</loc>", r.text)
        hits = [u for u in locs if re.search(r"invest|result|report|newsroom|rns",
                                             u, re.I)]
        block("WEIR SITEMAP", f"HTTP {r.status_code} locs={len(locs)}\n"
              + "\n".join(hits[:80]))
    except Exception as exc:  # noqa: BLE001
        block("WEIR SITEMAP ERROR", repr(exc))


def investegate() -> None:
    for name, url in [
        ("company", "https://www.investegate.co.uk/company/WEIR"),
        ("search", "https://www.investegate.co.uk/announcements?company=WEIR"),
    ]:
        try:
            r = creq_get(url)
            links = re.findall(r'href="([^"]+announcement[^"]+)"[^>]*>([^<]{0,120})',
                               r.text)
            block(f"INVESTEGATE {name}", f"HTTP {r.status_code} len={len(r.text)}\n"
                  + "\n".join(f"{h} | {t.strip()}" for h, t in links[:40]))
        except Exception as exc:  # noqa: BLE001
            block(f"INVESTEGATE {name} ERROR", repr(exc))


def main() -> int:
    fls_titles()
    weir_sitemap()
    investegate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
