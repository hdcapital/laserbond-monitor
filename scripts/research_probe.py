"""One-off research probe round 2 (runs in CI where the network is open).

Candidates under test, to be correlated against actual LBL segment history
BEFORE any ingester is built:
  1. Weir Group (WEIR.L) / FLSmidth (FLS.CO) monthly closes - Yahoo chart
     API with the cookie+crumb flow and retry/backoff (plain GET got 429).
  2. NSW coal volumes:
     a. ABS SDMX - search dataflows for merchandise-export / coal flows
        (state-level tonnes/values would ride on our existing ABS client).
     b. PWCS performance pages (Hunter Valley coal loadings).
     c. Port of Newcastle trade pages via curl_cffi impersonation
        (plain requests got 403).
     d. Coal Services NSW statistics page via impersonation (403 plain;
        page suggests reports are paid - verify).

Prints machine-parseable blocks to stdout; nothing is written to the store.
Delete this script once the research round is done.
"""
from __future__ import annotations

import datetime as dt
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


def yahoo_session() -> tuple[requests.Session, str]:
    s = requests.Session()
    s.headers.update(UA)
    try:
        s.get("https://fc.yahoo.com", timeout=20)
    except Exception:  # noqa: BLE001 - only needed for the cookie
        pass
    crumb = ""
    try:
        r = s.get("https://query2.finance.yahoo.com/v1/test/getcrumb", timeout=20)
        if r.status_code == 200:
            crumb = r.text.strip()
    except Exception:  # noqa: BLE001
        pass
    return s, crumb


def yahoo_monthly(s: requests.Session, crumb: str, symbol: str) -> None:
    last_err = "no attempt"
    for host in ("query2", "query1"):
        for attempt in range(3):
            url = (f"https://{host}.finance.yahoo.com/v8/finance/chart/{symbol}"
                   f"?range=20y&interval=1mo")
            if crumb:
                url += f"&crumb={crumb}"
            try:
                r = s.get(url, timeout=30)
                if r.status_code == 429:
                    last_err = f"429 on {host} attempt {attempt}"
                    time.sleep(8 * (attempt + 1))
                    continue
                r.raise_for_status()
                res = r.json()["chart"]["result"][0]
                ts = res["timestamp"]
                closes = res["indicators"]["adjclose"][0]["adjclose"]
                lines = ["date,adjclose"]
                for t, c in zip(ts, closes):
                    if c is not None:
                        d = dt.datetime.utcfromtimestamp(t).strftime("%Y-%m-%d")
                        lines.append(f"{d},{round(float(c), 4)}")
                block(f"YAHOO {symbol}", "\n".join(lines))
                return
            except Exception as exc:  # noqa: BLE001
                last_err = repr(exc)
                time.sleep(5)
    block(f"YAHOO {symbol} ERROR", last_err)


def abs_flow_search() -> None:
    try:
        r = requests.get("https://data.api.abs.gov.au/rest/dataflow/ABS?references=none",
                         headers={**UA, "Accept": "application/xml"}, timeout=60)
        hits = []
        for m in re.finditer(r'id="([^"]+)"[^>]*>\s*<[^>]*Name[^>]*>([^<]+)', r.text):
            fid, name = m.group(1), m.group(2)
            if re.search(r"merch|trade|export|coal|mineral", fid + " " + name, re.I):
                hits.append(f"{fid} | {name}")
        block("ABS FLOWS", f"HTTP {r.status_code}\n" + "\n".join(hits[:80]))
    except Exception as exc:  # noqa: BLE001
        block("ABS FLOWS ERROR", repr(exc))


def abs_structure(flow: str) -> None:
    try:
        r = requests.get(
            f"https://data.api.abs.gov.au/rest/datastructure/ABS/{flow}"
            f"?references=children",
            headers={**UA, "Accept": "application/xml"}, timeout=60)
        dims = re.findall(r'<str:Dimension id="([^"]+)"', r.text)
        cls = re.findall(r'<str:Codelist id="([^"]+)"', r.text)
        block(f"ABS STRUCTURE {flow}",
              f"HTTP {r.status_code}\ndims: {dims}\ncodelists: {cls[:30]}")
    except Exception as exc:  # noqa: BLE001
        block(f"ABS STRUCTURE {flow} ERROR", repr(exc))


def impersonated(name: str, url: str) -> None:
    try:
        from curl_cffi import requests as creq
        r = creq.get(url, impersonate="chrome", timeout=40)
        html = r.text
        links = re.findall(r'href="([^"]+)"[^>]*>([^<]{0,120})', html)
        interesting = [f"{h} | {t.strip()}" for h, t in links
                       if re.search(r"xlsx|xls|pdf|csv|statistic|report|trade|"
                                    r"export|volume|throughput|performance|coal",
                                    h + " " + t, re.I)]
        block(f"IMP {name} {url}",
              f"HTTP {r.status_code} len={len(html)}\n" + "\n".join(interesting[:100]))
    except Exception as exc:  # noqa: BLE001
        block(f"IMP {name} {url} ERROR", repr(exc))


def main() -> int:
    s, crumb = yahoo_session()
    block("YAHOO CRUMB", crumb or "(none)")
    for sym in ("WEIR.L", "FLS.CO"):
        yahoo_monthly(s, crumb, sym)
    abs_flow_search()
    abs_structure("MERCH_EXP")
    impersonated("pwcs-performance", "https://pwcs.com.au/sustainability/our-performance")
    impersonated("pwcs-annual", "https://pwcs.com.au/about/annual-reports")
    impersonated("pon-trade", "https://www.portofnewcastle.com.au/trade/")
    impersonated("pon-root", "https://www.portofnewcastle.com.au/")
    impersonated("coalservices-stats",
                 "https://www.coalservices.com.au/statistics/nsw-coal-industry-statistics/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
