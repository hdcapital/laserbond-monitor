"""One-off research probe (runs in CI where the network is open).

Fetches CANDIDATE series for new pulse inputs so they can be correlated
against actual LBL segment history BEFORE any ingester is built:

  1. Weir Group (WEIR.L) and FLSmidth (FLS.CO) monthly share prices
     (Products segment: LBL's two dominant customers).
  2. Endpoint discovery for NSW coal volume sources: Coal Services NSW,
     Port Waratah Coal Services (PWCS), Port of Newcastle.

Prints machine-parseable blocks to stdout; nothing is written to the store.
Delete this script once the research round is done.
"""
from __future__ import annotations

import json
import re
import sys

import requests

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"}


def block(name: str, text: str) -> None:
    print(f"\n===BEGIN {name}===")
    print(text)
    print(f"===END {name}===")


def yahoo_monthly(symbol: str) -> None:
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?range=20y&interval=1mo")
    try:
        r = requests.get(url, headers=UA, timeout=30)
        r.raise_for_status()
        res = r.json()["chart"]["result"][0]
        ts = res["timestamp"]
        closes = res["indicators"]["adjclose"][0]["adjclose"]
        lines = ["date,adjclose"]
        import datetime as dt
        for t, c in zip(ts, closes):
            if c is not None:
                d = dt.datetime.utcfromtimestamp(t).strftime("%Y-%m-%d")
                lines.append(f"{d},{round(float(c), 4)}")
        block(f"YAHOO {symbol}", "\n".join(lines))
    except Exception as exc:  # noqa: BLE001
        block(f"YAHOO {symbol} ERROR", repr(exc))


def stooq_monthly(symbol: str) -> None:
    url = f"https://stooq.com/q/d/l/?s={symbol}&i=m"
    try:
        r = requests.get(url, headers=UA, timeout=30)
        body = r.text.strip()
        block(f"STOOQ {symbol}", body[:8000] if body else f"HTTP {r.status_code} empty")
    except Exception as exc:  # noqa: BLE001
        block(f"STOOQ {symbol} ERROR", repr(exc))


def discover(name: str, url: str) -> None:
    try:
        r = requests.get(url, headers=UA, timeout=30)
        html = r.text
        links = re.findall(r'href="([^"]+)"[^>]*>([^<]{0,120})', html)
        interesting = [f"{h} | {t.strip()}" for h, t in links
                       if re.search(r"xlsx|xls|pdf|csv|statistic|report|trade|"
                                    r"export|volume|throughput|performance",
                                    h + " " + t, re.I)]
        head = f"HTTP {r.status_code} len={len(html)}\n"
        block(f"DISCOVER {name} {url}", head + "\n".join(interesting[:120]))
    except Exception as exc:  # noqa: BLE001
        block(f"DISCOVER {name} {url} ERROR", repr(exc))


def main() -> int:
    for sym in ("WEIR.L", "FLS.CO"):
        yahoo_monthly(sym)
    for sym in ("weir.uk", "fls.dk"):
        stooq_monthly(sym)
    discover("coalservices", "https://www.coalservices.com.au/mining/statistics/")
    discover("coalservices-root", "https://www.coalservices.com.au/")
    discover("pwcs", "https://pwcs.com.au/")
    discover("pwcs-exports", "https://pwcs.com.au/exports/")
    discover("port-newcastle", "https://www.portofnewcastle.com.au/trade/")
    discover("port-newcastle-root", "https://www.portofnewcastle.com.au/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
