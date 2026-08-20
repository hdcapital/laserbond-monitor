"""One-off research probe round 4 (runs in CI where the network is open).

  1. FLSmidth price history - Nasdaq Nordic webproxy (official exchange).
  2. Weir price history - Boerse Frankfurt public API (official exchange).
  3. Port of Newcastle trade-report PDF - text sample to design a parser.
  4. ABS MERCH_EXP codelists - find the coal SITC code and state codes.
  5. Weir / FLSmidth IR pages - discover results/report PDF feeds
     (order intake is the preferred fundamental signal).

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


def nasdaq_nordic_fls() -> None:
    # Official Nasdaq Nordic data feed used by their own charts.
    url = ("https://www.nasdaqomxnordic.com/webproxy/DataFeedProxy.aspx"
           "?SubSystem=History&Action=GetDataSeries&AppendIntraDay=no"
           "&Instrument=CSE3456&FromDate=2007-01-01&ToDate=2026-08-20"
           "&hi__a=0,1,2,4,21,8,10,12,9&OmitNoTrade=true&ext_xslt=/nordicV3/"
           "hi_csv.xsl&ext_xslt_options=,,space&ext_xslt_lang=en"
           "&ext_xslt_hiddenattrs=,ip,iv,&ext_contenttype=application/text")
    try:
        r = requests.get(url, headers=UA, timeout=60)
        lines = r.text.strip().splitlines()
        head = f"HTTP {r.status_code} rows={len(lines)}\n"
        block("NASDAQ FLS CSE3456",
              head + "\n".join(lines[:6] + ["..."] + lines[-4:]) if len(lines) > 12
              else head + r.text[:2000])
    except Exception as exc:  # noqa: BLE001
        block("NASDAQ FLS ERROR", repr(exc))
    # instrument-id discovery fallback
    try:
        r = requests.get("https://www.nasdaqomxnordic.com/webproxy/DataFeedProxy.aspx"
                         "?SubSystem=Prices&Action=Search&InstrumentName=FLSmidth"
                         "&ext_xslt=/nordicV3/inst_search.xsl", headers=UA, timeout=40)
        block("NASDAQ SEARCH FLSmidth", f"HTTP {r.status_code}\n{r.text[:2500]}")
    except Exception as exc:  # noqa: BLE001
        block("NASDAQ SEARCH ERROR", repr(exc))


def boerse_frankfurt(name: str, isin: str, mic: str = "XFRA") -> None:
    # Public API behind boerse-frankfurt.de; needs trace headers.
    url = (f"https://api.boerse-frankfurt.de/v1/data/price_history"
           f"?limit=5000&offset=0&isin={isin}&mic={mic}"
           f"&minDate=2007-01-01&maxDate=2026-08-20&cleanSplit=false"
           f"&cleanPayout=false&cleanSubscriptionRights=false")
    client_date = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    salt = "w4icATTGtnjrZMmS"
    trace = hashlib.md5((client_date + url + salt).encode()).hexdigest()
    headers = {**UA, "Client-Date": client_date, "X-Client-TraceId": trace,
               "X-Security": hashlib.md5(client_date.encode()).hexdigest(),
               "Accept": "application/json"}
    try:
        r = requests.get(url, headers=headers, timeout=60)
        block(f"BOERSE-FFM {name} {isin} {mic}",
              f"HTTP {r.status_code}\n{r.text[:2500]}")
    except Exception as exc:  # noqa: BLE001
        block(f"BOERSE-FFM {name} ERROR", repr(exc))


def pon_pdf_sample() -> None:
    url = "https://pon.com.au/wp-content/uploads/2026/08/Trade-Report-July-2026.pdf"
    old = ("https://pon.com.au/wp-content/uploads/2022/01/"
           "20210101-External-Monthly-Trade-Report-Jan-2021.pdf")
    for label, u in (("2026-07", url), ("2021-01", old)):
        try:
            from curl_cffi import requests as creq
            r = creq.get(u, impersonate="chrome", timeout=60)
            from io import BytesIO

            from pypdf import PdfReader
            reader = PdfReader(BytesIO(r.content))
            text = "\n".join((p.extract_text() or "") for p in reader.pages[:4])
            m = re.findall(r"[^\n]*[Cc]oal[^\n]*", text)
            block(f"PON PDF {label}",
                  f"HTTP {r.status_code} pages={len(reader.pages)}\n"
                  f"COAL LINES:\n" + "\n".join(m[:25]) +
                  "\n----- first 2500 chars -----\n" + text[:2500])
        except Exception as exc:  # noqa: BLE001
            block(f"PON PDF {label} ERROR", repr(exc))


def abs_merch_codelists() -> None:
    try:
        r = requests.get("https://data.api.abs.gov.au/rest/datastructure/ABS/"
                         "MERCH_EXP?references=children",
                         headers={**UA, "Accept": "application/xml"}, timeout=120)
        xml = r.text
        out = [f"HTTP {r.status_code} len={len(xml)}"]
        for clm in re.finditer(r'<structure:Codelist id="([^"]+)"[^>]*>\s*'
                               r'<common:Name[^>]*>([^<]+)', xml):
            out.append(f"CODELIST {clm.group(1)} | {clm.group(2)}")
        # codes mentioning coal
        for cm in re.finditer(r'<structure:Code id="([^"]+)">\s*(?:<common:Annotations>.*?'
                              r'</common:Annotations>\s*)?<common:Name[^>]*>([^<]{0,120})',
                              xml, re.S):
            if re.search(r"coal|coke|briquette", cm.group(2), re.I):
                out.append(f"COALCODE {cm.group(1)} | {cm.group(2)}")
        # state-ish codes
        sm = re.search(r'<structure:Codelist id="CL_STATE".*?</structure:Codelist>',
                       xml, re.S)
        if sm:
            for cm in re.finditer(r'<structure:Code id="([^"]+)">\s*(?:<common:Annotations>'
                                  r'.*?</common:Annotations>\s*)?<common:Name[^>]*>([^<]+)',
                                  sm.group(0), re.S):
                out.append(f"STATE {cm.group(1)} | {cm.group(2)}")
        dims = re.findall(r'<structure:Dimension id="([^"]+)"[^>]*position="(\d+)"', xml)
        out.append(f"DIMS {dims}")
        block("ABS MERCH_EXP CODELISTS", "\n".join(out[:120]))
    except Exception as exc:  # noqa: BLE001
        block("ABS MERCH_EXP CODELISTS ERROR", repr(exc))


def discover(name: str, url: str, pat: str) -> None:
    try:
        from curl_cffi import requests as creq
        r = creq.get(url, impersonate="chrome", timeout=40)
        links = re.findall(r'href="([^"]+)"[^>]*>([^<]{0,140})', r.text)
        hits = [f"{h} | {t.strip()}" for h, t in links if re.search(pat, h + t, re.I)]
        block(f"DISCOVER {name}", f"HTTP {r.status_code}\n" + "\n".join(hits[:60]))
    except Exception as exc:  # noqa: BLE001
        block(f"DISCOVER {name} ERROR", repr(exc))


def main() -> int:
    nasdaq_nordic_fls()
    boerse_frankfurt("WEIR", "GB0009465807")
    boerse_frankfurt("FLS", "DK0010234467")
    pon_pdf_sample()
    abs_merch_codelists()
    discover("weir-results", "https://www.global.weir/investors/results-reports/",
             r"pdf|result|interim|annual|report")
    discover("fls-reports", "https://www.flsmidth.com/en-gb/company/investors/"
             "reports-and-presentations", r"pdf|report|quarter|interim|annual")
    return 0


if __name__ == "__main__":
    sys.exit(main())
