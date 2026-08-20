"""One-off research probe round 6 (runs in CI where the network is open).

  1. ABS MERCH_EXP 32.TOT.1.M (NSW coal export values, monthly 1995->) -
     print the FULL csv so the correlation gate can run locally.
     Also pull QLD (state 3) for comparison.
  2. FLSmidth financial-downloads - PDF links WITH surrounding context so
     quarterly reports can be identified for order-intake extraction.
  3. Weir investors pages - identify the IR tools provider (euroland/q4/
     investis) and any results-PDF archive.

Prints machine-parseable blocks to stdout; nothing is written to the store.
Delete this script once the research round is done.
"""
from __future__ import annotations

import re
import sys

import requests

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"}


def block(name: str, text: str) -> None:
    print(f"\n===BEGIN {name}===")
    print(text)
    print(f"===END {name}===")


def abs_full(state: str, label: str) -> None:
    url = (f"https://data.api.abs.gov.au/rest/data/ABS,MERCH_EXP,1.0.0/"
           f"32.TOT.{state}.M?startPeriod=1990-01"
           f"&dimensionAtObservation=AllDimensions&format=csv")
    try:
        r = requests.get(url, headers={**UA, "Accept": "text/csv"}, timeout=120)
        block(f"ABS FULL {label}", f"HTTP {r.status_code}\n{r.text.strip()}")
    except Exception as exc:  # noqa: BLE001
        block(f"ABS FULL {label} ERROR", repr(exc))


def fls_links_with_context() -> None:
    try:
        from curl_cffi import requests as creq
        r = creq.get("https://www.flsmidth.com/en/investors/financial-downloads",
                     impersonate="chrome", timeout=60)
        html = r.text
        out = []
        for m in re.finditer(r'href="(https?://[^"]+\.pdf)"', html):
            start = max(0, m.start() - 400)
            ctx = re.sub(r"<[^>]+>", " ", html[start:m.start()])
            ctx = re.sub(r"\s+", " ", ctx).strip()[-140:]
            out.append(f"{m.group(1)} ||| {ctx}")
        block("FLS PDF LINKS", f"HTTP {r.status_code} n={len(out)}\n"
              + "\n".join(out[:80]))
    except Exception as exc:  # noqa: BLE001
        block("FLS PDF LINKS ERROR", repr(exc))


def weir_provider() -> None:
    try:
        from curl_cffi import requests as creq
        for name, url in [
            ("investors", "https://www.global.weir/investors/"),
            ("results", "https://www.global.weir/investors/results-and-presentations/"),
            ("reports", "https://www.global.weir/investors/annual-report/"),
        ]:
            r = creq.get(url, impersonate="chrome", timeout=60)
            html = r.text
            srcs = re.findall(r'(?:src|href)="([^"]*(?:euroland|q4cdn|q4inc|'
                              r'investis|sharegraph|halo|cision|globenewswire|'
                              r'precisionir|api)[^"]*)"', html, re.I)
            pdfs = re.findall(r'href="([^"]+\.pdf)"', html)[:25]
            block(f"WEIR {name}", f"HTTP {r.status_code} len={len(html)}\n"
                  f"PROVIDERS: {srcs[:15]}\nPDFS: " + "\n".join(pdfs))
    except Exception as exc:  # noqa: BLE001
        block("WEIR PROVIDER ERROR", repr(exc))


def main() -> int:
    abs_full("1", "NSW-COAL")
    abs_full("3", "QLD-COAL")
    fls_links_with_context()
    weir_provider()
    return 0


if __name__ == "__main__":
    sys.exit(main())
