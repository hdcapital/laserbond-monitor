"""One-off research probe round 5 (runs in CI where the network is open).

  1. Boerse Frankfurt: instrument search for Weir/FLSmidth -> correct
     isin/mic, then price_history sample.
  2. FLSmidth financial-downloads: list actual report PDF links.
  3. Weir investors section + sitemap: find the results/reports page.
  4. ABS MERCH_EXP: state codelist + a real data sample for SITC 32
     (coal) by state, monthly.

Prints machine-parseable blocks to stdout; nothing is written to the store.
Delete this script once the research round is done.
"""
from __future__ import annotations

import hashlib
import re
import sys
import time

import requests

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"}


def block(name: str, text: str) -> None:
    print(f"\n===BEGIN {name}===")
    print(text)
    print(f"===END {name}===")


def bf_headers(url: str) -> dict:
    client_date = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    salt = "w4icATTGtnjrZMmS"
    return {**UA, "Client-Date": client_date,
            "X-Client-TraceId": hashlib.md5((client_date + url + salt).encode()).hexdigest(),
            "X-Security": hashlib.md5(client_date.encode()).hexdigest(),
            "Accept": "application/json"}


def bf_get(name: str, url: str, grab: int = 2500) -> None:
    try:
        r = requests.get(url, headers=bf_headers(url), timeout=60)
        block(name, f"HTTP {r.status_code}\n{r.text[:grab]}")
    except Exception as exc:  # noqa: BLE001
        block(f"{name} ERROR", repr(exc))


def discover(name: str, url: str, pat: str, grab: int = 80) -> None:
    try:
        from curl_cffi import requests as creq
        r = creq.get(url, impersonate="chrome", timeout=40)
        links = re.findall(r'href="([^"]+)"[^>]*>([^<]{0,140})', r.text)
        hits = [f"{h} | {t.strip()}" for h, t in links if re.search(pat, h + t, re.I)]
        block(f"DISCOVER {name}", f"HTTP {r.status_code} len={len(r.text)}\n"
              + "\n".join(hits[:grab]))
    except Exception as exc:  # noqa: BLE001
        block(f"DISCOVER {name} ERROR", repr(exc))


def abs_state_codes_and_sample() -> None:
    try:
        r = requests.get("https://data.api.abs.gov.au/rest/codelist/ABS/"
                         "CL_MERCH_STATE", headers={**UA, "Accept": "application/xml"},
                         timeout=60)
        codes = re.findall(r'<structure:Code id="([^"]+)">\s*(?:<common:Annotations>.*?'
                           r'</common:Annotations>\s*)?<common:Name[^>]*>([^<]+)',
                           r.text, re.S)
        block("ABS CL_MERCH_STATE", f"HTTP {r.status_code}\n"
              + "\n".join(f"{c} | {n}" for c, n in codes[:20]))
        nsw = next((c for c, n in codes if "New South Wales" in n), None)
    except Exception as exc:  # noqa: BLE001
        block("ABS CL_MERCH_STATE ERROR", repr(exc))
        nsw = None
    key = f"32.TOT.{nsw or '1'}.M"
    url = (f"https://data.api.abs.gov.au/rest/data/ABS,MERCH_EXP,1.0.0/{key}"
           f"?startPeriod=1990-01&dimensionAtObservation=AllDimensions&format=csv")
    try:
        r = requests.get(url, headers={**UA, "Accept": "text/csv"}, timeout=120)
        lines = r.text.strip().splitlines()
        block(f"ABS MERCH_EXP DATA {key}",
              f"HTTP {r.status_code} rows={len(lines)}\n"
              + "\n".join(lines[:6] + ["..."] + lines[-4:]))
    except Exception as exc:  # noqa: BLE001
        block("ABS MERCH_EXP DATA ERROR", repr(exc))


def main() -> int:
    for term in ("Weir", "FLSmidth"):
        u = (f"https://api.boerse-frankfurt.de/v1/global_search/limitedsearch"
             f"?searchTerms={term}")
        bf_get(f"BF SEARCH {term}", u)
    # direct price_history attempts on XETR
    for name, isin in (("WEIR", "GB0009465807"), ("FLS", "DK0010234467")):
        for mic in ("XETR", "XFRA"):
            u = (f"https://api.boerse-frankfurt.de/v1/data/price_history"
                 f"?limit=10&offset=0&isin={isin}&mic={mic}"
                 f"&minDate=2026-06-01&maxDate=2026-08-20")
            bf_get(f"BF HIST {name} {mic}", u, 1200)
    discover("fls-downloads",
             "https://www.flsmidth.com/en/investors/financial-downloads",
             r"\.pdf|report|quarter|interim|annual", 60)
    discover("weir-investors", "https://www.global.weir/investors/",
             r"result|report|rns|presentation", 60)
    discover("weir-sitemap", "https://www.global.weir/sitemap.xml",
             r"invest|result|report", 60)
    abs_state_codes_and_sample()
    return 0


if __name__ == "__main__":
    sys.exit(main())
