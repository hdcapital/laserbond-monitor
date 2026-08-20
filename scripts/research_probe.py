"""One-off research probe round 3 (runs in CI where the network is open).

  1. Weir (XLON:WEIR) / FLSmidth (XCSE:FLS) price history via WSJ and
     MarketWatch CSV download endpoints (Yahoo 429s runner IPs).
  2. Port of Newcastle trade reports page (pon.com.au) - monthly volumes?
  3. ABS dataflow catalogue - raw XML samples + proper flow search for
     state-level coal/export series.

Prints machine-parseable blocks to stdout; nothing is written to the store.
Delete this script once the research round is done.
"""
from __future__ import annotations

import re
import sys

import requests

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
      "Accept": "text/html,application/xhtml+xml,text/csv,*/*"}


def block(name: str, text: str) -> None:
    print(f"\n===BEGIN {name}===")
    print(text)
    print(f"===END {name}===")


def wsj(name: str, country: str, exch: str, sym: str,
        start: str, end: str) -> None:
    url = (f"https://www.wsj.com/market-data/quotes/{country}/{exch}/{sym}"
           f"/historical-prices/download?MOD=mw_quote"
           f"&startDate={start}&endDate={end}")
    try:
        r = requests.get(url, headers=UA, timeout=40)
        body = r.text.strip()
        lines = body.splitlines()
        head = f"HTTP {r.status_code} rows={len(lines)}\n"
        sample = "\n".join(lines[:5] + ["..."] + lines[-5:]) if len(lines) > 12 \
            else body[:2000]
        block(f"WSJ {name} {start}..{end}", head + sample)
    except Exception as exc:  # noqa: BLE001
        block(f"WSJ {name} ERROR", repr(exc))


def marketwatch(name: str, sym: str, country: str,
                start: str, end: str) -> None:
    url = (f"https://www.marketwatch.com/investing/stock/{sym}"
           f"/downloaddatapartial?startdate={start}%2000:00:00&enddate={end}"
           f"%2000:00:00&daterange=d30&frequency=p1d&csvdownload=true"
           f"&downloadpartial=false&newdates=false&countrycode={country}")
    try:
        r = requests.get(url, headers=UA, timeout=40)
        body = r.text.strip()
        lines = body.splitlines()
        head = f"HTTP {r.status_code} rows={len(lines)}\n"
        sample = "\n".join(lines[:5] + ["..."] + lines[-5:]) if len(lines) > 12 \
            else body[:2000]
        block(f"MW {name} {start}..{end}", head + sample)
    except Exception as exc:  # noqa: BLE001
        block(f"MW {name} ERROR", repr(exc))


def impersonated(name: str, url: str) -> None:
    try:
        from curl_cffi import requests as creq
        r = creq.get(url, impersonate="chrome", timeout=40)
        html = r.text
        links = re.findall(r'href="([^"]+)"[^>]*>([^<]{0,140})', html)
        interesting = [f"{h} | {t.strip()}" for h, t in links
                       if re.search(r"xlsx|xls|pdf|csv|report|trade|volume|"
                                    r"throughput|month|coal|cargo|statistic",
                                    h + " " + t, re.I)]
        block(f"IMP {name} {url}",
              f"HTTP {r.status_code} len={len(html)}\n" + "\n".join(interesting[:100]))
    except Exception as exc:  # noqa: BLE001
        block(f"IMP {name} {url} ERROR", repr(exc))


def abs_raw(name: str, url: str, grab: int = 3000) -> str:
    try:
        r = requests.get(url, headers={**UA, "Accept": "application/xml"},
                         timeout=90)
        block(f"ABS RAW {name}", f"HTTP {r.status_code}\n{r.text[:grab]}")
        return r.text
    except Exception as exc:  # noqa: BLE001
        block(f"ABS RAW {name} ERROR", repr(exc))
        return ""


def main() -> int:
    wsj("WEIR", "UK", "XLON", "WEIR", "01/01/2007", "08/20/2026")
    wsj("FLS", "DK", "XCSE", "FLS", "01/01/2007", "08/20/2026")
    marketwatch("WEIR", "weir", "uk", "01/01/2024", "08/20/2026")
    marketwatch("FLS", "fls", "dk", "01/01/2024", "08/20/2026")

    impersonated("pon-trade-reports",
                 "https://pon.com.au/trade-and-business/trade-overview-reports/")

    xml = abs_raw("dataflows", "https://data.api.abs.gov.au/rest/dataflow/ABS"
                               "?references=none", 1500)
    if xml:
        flows = re.findall(
            r'<structure:Dataflow[^>]*id="([^"]+)".*?<common:Name[^>]*>([^<]+)',
            xml, re.S)
        if not flows:
            flows = re.findall(r'Dataflow[^>]*id="([^"]+)"()', xml)
        hits = [f"{fid} | {nm}" for fid, nm in flows
                if re.search(r"merch|trade|export|coal|mineral|commod",
                             fid + " " + nm, re.I)]
        block("ABS FLOW HITS", "\n".join(hits[:100]) or f"(none of {len(flows)})")
    abs_raw("MERCH_EXP structure",
            "https://data.api.abs.gov.au/rest/datastructure/ABS/MERCH_EXP"
            "?references=children", 4000)
    return 0


if __name__ == "__main__":
    sys.exit(main())
