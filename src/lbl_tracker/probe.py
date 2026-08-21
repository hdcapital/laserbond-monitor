"""Live source diagnostics for the CI verify loop.

`lbl-tracker probe [source ...]` fetches each source's real endpoints and
prints bounded excerpts of the raw responses, so ingester parsing can be
verified/fixed against reality. Never writes to the store.
"""
from __future__ import annotations

import io
import json
import sys
import traceback

import pandas as pd

from .http import get, make_session

LINE = "=" * 78


def _p(*args):
    print(*args, flush=True)


def _head(text: str, n: int = 1200) -> str:
    return str(text)[:n].replace("\r", "")


def probe_abs():
    from .ingest import abs_sdmx
    flows = abs_sdmx.list_dataflows()
    _p(f"ABS dataflows: {len(flows)} total")
    for kw in ("capital expenditure", "capex", "exploration"):
        hits = abs_sdmx.find_dataflows(kw)
        _p(f"-- flows matching {kw!r}:")
        _p(hits.to_string(max_rows=30))
    for flow in ("CAPEX", "MIN_EXP"):
        try:
            df = abs_sdmx.get_data(flow, "all", params={"startPeriod": "2023"})
            _p(f"-- {flow}: {len(df)} obs since 2023; columns={list(df.columns)}")
            for col in df.columns:
                if col.endswith("_name"):
                    _p(f"   {col}: {sorted(map(str, df[col].dropna().unique()))[:25]}")
            if flow == "MIN_EXP" and "MEASURE_name" in df.columns:
                metres = df[df["MEASURE_name"] == "Metres drilled"]
                combos = metres.groupby([c for c in ("REGION_name", "DEPOSIT_TYPE_name",
                                                     "MINERAL_TYPE_name", "TSEST_name")
                                         if c in metres.columns]).size()
                _p("   Metres-drilled combos:")
                _p(combos.to_string()[:3000])
            _p(df.head(8).to_string())
        except Exception as exc:  # noqa: BLE001
            _p(f"-- {flow} fetch failed: {exc}")


def probe_qld_coal():
    from .ingest import qld_coal
    session = make_session()
    resp = get(f"{qld_coal.CKAN}/package_search", session=session,
               params={"q": "coal industry review", "rows": 25})
    results = resp.json()["result"]["results"]
    _p(f"CKAN packages: {len(results)}")
    for pkg in results:
        _p(f"- package: {pkg.get('name')} | title: {pkg.get('title')}")
        for res in pkg.get("resources", [])[:30]:
            _p(f"    res: [{res.get('format')}] {res.get('name')} -> {res.get('url')}")
    found = qld_coal.discover_resources(session)
    _p(f"discovered resources: {json.dumps(found, indent=1)[:3000]}")
    if found:
        res = found[0]
        content = qld_coal._download(res["url"], session)
        _p(f"workbook bytes: {len(content)}")
        book = pd.read_excel(io.BytesIO(content), sheet_name=None, header=None,
                             dtype=str)
        for sheet, raw in list(book.items())[:5]:
            _p(f"-- sheet {sheet!r} shape={raw.shape}")
            _p(raw.head(14).iloc[:, :12].to_string()[:2800])
        long = qld_coal.parse_workbook(content, res["url"])
        _p(f"long rows: {len(long)}")
        if len(long):
            _p(f"range {long['date'].min()}..{long['date'].max()}")
            _p(long.tail(8).to_string())


def probe_pilbara():
    from bs4 import BeautifulSoup

    from .ingest import pilbara_ports
    session = make_session()
    try:
        robots = get(f"{pilbara_ports.BASE}/robots.txt", session=session).text
        _p(f"robots.txt: {_head(robots, 800)}")
    except Exception as exc:  # noqa: BLE001
        _p(f"robots.txt failed: {exc}")
    urls = pilbara_ports.sitemap_urls(session)
    _p(f"sitemap urls: {len(urls)}")
    news = [u for u in urls if "/news/" in u]
    _p(f"news urls: {len(news)}; sample: {news[:15]}")
    items = pilbara_ports.list_statements(session)
    _p(f"articles: {len(items)}")
    trade = [i for i in items if pilbara_ports.TRADE_TITLE.search(i["title"])]
    _p(f"trade-like: {len(trade)}")
    for item in trade[:10]:
        _p(f"- {item['title']!r} -> {item['url']}")
    for item in trade[:3]:
        text = BeautifulSoup(get(item["url"], session=session).text,
                             "lxml").get_text(" ", strip=True)
        _p(f"article text excerpt ({item['url']}):")
        _p(_head(" ".join(text.split()), 2500))
        parsed = pilbara_ports.parse_statement(item["url"], item["title"], session)
        _p(f"parsed rows: {parsed}")


def probe_rba():
    from .ingest import rba
    session = make_session()
    idx = get("https://www.rba.gov.au/statistics/tables/", session=session).text
    import re
    links = sorted(set(re.findall(r'href="([^"]*(?:csv|xls)[^"]*)"', idx)))
    _p(f"RBA table links ({len(links)}):")
    for link in links:
        if any(k in link.lower() for k in ("i2", "f11", "commodity", "exchange")):
            _p(f"- {link}")
    for url in (rba.I2_URL, rba.F11_MONTHLY_URL, *rba.FX_DAILY_URLS):
        try:
            text = get(url, session=session).text
            _p(f"-- {url}")
            _p(_head(text, 1500))
        except Exception as exc:  # noqa: BLE001
            _p(f"-- {url} FAILED: {exc}")


def probe_aisi():
    from .ingest import aisi
    session = make_session()
    html = get(aisi.DATA_PAGE, session=session).text
    from bs4 import BeautifulSoup
    text = " ".join(BeautifulSoup(html, "lxml").get_text(" ", strip=True).split())
    _p("industry-data page text excerpt:")
    _p(_head(text, 4000))
    urls = aisi.collect_release_urls(session)
    _p(f"release urls: {urls[:15]}")
    hit = aisi.parse_release_text(text, aisi.DATA_PAGE)
    _p(f"parsed from data page: {hit}")


def probe_fred():
    from .http import get as _get
    candidates = ["AISMNO", "A31SNO", "ANOSIS", "IRONSTEELNO", "AMDMNO"]
    for series in candidates:
        try:
            resp = _get("https://fred.stlouisfed.org/graph/fredgraph.csv",
                        params={"id": series})
            head = resp.text.splitlines()
            _p(f"-- {series}: {head[:2]} ... last={head[-1] if head else '?'} "
               f"({len(head)} lines)")
        except Exception as exc:  # noqa: BLE001
            _p(f"-- {series}: FAILED {exc}")
    try:
        resp = _get("https://fred.stlouisfed.org/searchresults/",
                    params={"nq": "new orders iron steel mills"})
        import re
        ids = re.findall(r'href="/series/([A-Z0-9]+)"', resp.text)
        _p(f"search result series ids: {list(dict.fromkeys(ids))[:25]}")
    except Exception as exc:  # noqa: BLE001
        _p(f"search failed: {exc}")


def probe_baker_hughes():
    from bs4 import BeautifulSoup

    from .ingest import baker_hughes
    session = make_session()
    for page in baker_hughes.PAGES:
        try:
            soup = BeautifulSoup(get(page, session=session).text, "lxml")
            _p(f"-- anchors on {page}:")
            for a in soup.find_all("a", href=True)[:80]:
                text = " ".join(a.get_text(" ", strip=True).split())
                if text:
                    _p(f"   {text[:100]} -> {a['href'][:120]}")
        except Exception as exc:  # noqa: BLE001
            _p(f"-- {page} FAILED: {exc}")
    files = baker_hughes.discover_files(session)
    _p(f"resolved: {files}")
    for kind, urls in files.items():
        for url in urls[:2]:
            content = get(url, session=session).content
            try:
                book = pd.read_excel(io.BytesIO(content), sheet_name=None, header=None)
            except Exception as exc:  # noqa: BLE001
                _p(f"-- {kind} {url}: read_excel failed: {exc}")
                continue
            _p(f"-- {kind} {url} sheets: {list(book)}")
            for sheet in ("NAM Weekly", "NAM Monthly", "WW Monthly",
                          "Worldwide_Rigcount", "US Oil & Gas Split"):
                if sheet in book:
                    raw = book[sheet]
                    _p(f"   sheet {sheet!r} shape={raw.shape}")
                    _p(raw.head(14).iloc[:, :16].to_string()[:2600])
                    _p("   ...tail:")
                    _p(raw.tail(4).iloc[:, :16].to_string()[:900])


def probe_cat_edgar():
    import time as _time

    from bs4 import BeautifulSoup

    from .ingest import cat_edgar
    session = make_session(sec=True)
    old = cat_edgar.MAX_FILINGS
    cat_edgar.MAX_FILINGS = 400
    try:
        hits = cat_edgar.search_filings(session)
    finally:
        cat_edgar.MAX_FILINGS = old
    _p(f"8-K item-7.01 filings: {len(hits)}")
    if hits:
        _p(f"date range: {hits[-1]['file_date']} .. {hits[0]['file_date']}")
        _p(f"recent dates: {[h['file_date'] for h in hits[:12]]}")
    # dump monthly-era exhibits (CAT discontinued monthly dealer-stats
    # filings ~2017; recent 7.01s are quarterly earnings releases)
    monthly_era = [h for h in hits if str(h["file_date"]) < "2017-03-01"][:3]
    _p(f"monthly-era sample: {[h['file_date'] for h in monthly_era]}")
    for hit in monthly_era:
        try:
            url = cat_edgar.exhibit_url(hit, session)
            _p(f"== {hit['file_date']} exhibit: {url}")
            if not url:
                continue
            html = get(url, session=session, sec=True).text
            text = " ".join(BeautifulSoup(html, "lxml").get_text(" ", strip=True).split())
            _p(_head(text, 3500))
            try:
                tables = pd.read_html(io.StringIO(html))
                _p(f"tables: {len(tables)}")
                for i, table in enumerate(tables[:4]):
                    _p(f"-- table {i} shape={table.shape}")
                    _p(table.head(12).iloc[:, :10].to_string()[:2200])
            except ValueError as exc:
                _p(f"read_html: {exc}")
            _p(f"parse_exhibit -> {cat_edgar.parse_exhibit(html, url)}")
        except Exception as exc:  # noqa: BLE001
            _p(f"== {hit.get('file_date')}: FAILED {exc}")
        _time.sleep(0.2)
    # walk newest -> older until an exhibit mentions Resource Industries
    shown = 0
    for hit in hits[:40]:
        try:
            url = cat_edgar.exhibit_url(hit, session)
            if not url:
                _p(f"- {hit['file_date']}: no exhibit")
                continue
            html = get(url, session=session, sec=True).text
            text = " ".join(BeautifulSoup(html, "lxml").get_text(" ", strip=True).split())
            has_ri = "resource industries" in text.lower()
            _p(f"- {hit['file_date']} {url.rsplit('/',1)[-1]}: RI={has_ri} :: {text[:160]}")
            if has_ri:
                _p("exhibit text excerpt:")
                _p(_head(text, 4000))
                try:
                    tables = pd.read_html(io.StringIO(html))
                    _p(f"tables: {len(tables)}")
                    for i, table in enumerate(tables[:5]):
                        _p(f"-- table {i} shape={table.shape}")
                        _p(table.head(10).to_string()[:2000])
                except ValueError as exc:
                    _p(f"read_html: {exc}")
                parsed = cat_edgar.parse_exhibit(html, url)
                _p(f"parse_exhibit -> {parsed}")
                break
            shown += 1
        except Exception as exc:  # noqa: BLE001
            _p(f"- {hit.get('file_date')}: FAILED {exc}")
        _time.sleep(0.15)


def probe_jsa():
    from bs4 import BeautifulSoup

    from .http import get_impersonated
    from .ingest import jsa_ivi
    session = jsa_ivi.jsa_session()
    # the WAF-fronted site answers to a browser TLS fingerprint - dump the
    # IVI page's anchors to find where the workbooks actually live
    for page in jsa_ivi.PAGES:
        try:
            html = get_impersonated(page, timeout=60).text
            soup = BeautifulSoup(html, "lxml")
            _p(f"-- impersonated anchors on {page}:")
            for a in soup.find_all("a", href=True):
                text = " ".join(a.get_text(" ", strip=True).split())
                href = a["href"]
                if text or "file" in href.lower() or "download" in href.lower():
                    _p(f"   {text[:100]!r} -> {href[:140]}")
        except Exception as exc:  # noqa: BLE001
            _p(f"-- {page} FAILED: {exc}")
        break  # both PAGES resolve to the same document
    try:
        resp = get(f"{jsa_ivi.DATA_GOV_CKAN}/package_search", session=session,
                   params={"q": "internet vacancy index", "rows": 20})
        for pkg in resp.json()["result"]["results"]:
            _p(f"- data.gov.au package: {pkg.get('name')} | {pkg.get('title')}")
            for res in pkg.get("resources", [])[:20]:
                _p(f"    res: [{res.get('format')}] {res.get('name')} -> "
                   f"{str(res.get('url'))[:130]}")
    except Exception as exc:  # noqa: BLE001
        _p(f"data.gov.au search FAILED: {exc}")
    for page in jsa_ivi.PAGES:
        try:
            html = get(page, session=session).text
            import re
            links = re.findall(r'href="([^"]+\.xlsx?[^"]*)"', html, re.I)
            _p(f"-- {page}: xlsx links: {links[:25]}")
        except Exception as exc:  # noqa: BLE001
            _p(f"-- {page} FAILED: {exc}")
    try:
        url = jsa_ivi.discover_workbook(session)
        _p(f"resolved workbook: {url}")
        content = get_impersonated(url, timeout=120).content
        _p(f"workbook bytes: {len(content)}")
        book = pd.read_excel(io.BytesIO(content), sheet_name=None, header=None)
        _p(f"sheets: {list(book)}")
        for sheet, raw in list(book.items())[:4]:
            _p(f"-- sheet {sheet!r} shape={raw.shape}")
            _p(raw.head(10).iloc[:, :10].to_string()[:2400])
        df = jsa_ivi.parse_workbook(content, url)
        _p(f"parsed rows: {len(df)}; series: {sorted(df['series_id'].unique())[:12]}")
    except Exception as exc:  # noqa: BLE001
        _p(f"workbook discovery/parse failed: {exc}")


def probe_tungsten():
    from .ingest.tungsten_news import fetch_flags
    flags = fetch_flags()
    _p(f"flags fetched: {len(flags)}")
    _p(flags.head(10).to_string())


def probe_asx():
    from .ingest import asx_announcements as asx
    session = make_session()
    for ticker in ("LBL", "EHL"):
        for template in asx.LIST_ENDPOINTS:
            url = template.format(ticker=ticker, count=5, page=1)
            try:
                resp = get(url, session=session)
                _p(f"-- {url}")
                _p(_head(resp.text, 2500))
            except Exception as exc:  # noqa: BLE001
                _p(f"-- {url} FAILED: {exc}")
    base = "https://asx.api.markitdigital.com/asx-research/1.0/companies/LBL/announcements"
    token = "access_token=83ff96335c2d45a094df02a206a39ff4"  # public token on asx.com.au
    for variant in ("itemsPerPage=20", f"itemsPerPage=20&{token}", "pageSize=20",
                    "count=20", "limit=20", "itemsPerPage=5&page=2",
                    f"itemsPerPage=5&page=2&{token}", "itemsPerPage=5&pageNumber=2",
                    "itemsPerPage=5&offset=5"):
        try:
            payload = get(f"{base}?{variant}", session=session).json()
            items = payload.get("data", {}).get("items", [])
            first = (items[0].get("headline", "")[:40], str(items[0].get("date"))[:10]) \
                if items else None
            _p(f"-- variant {variant}: {len(items)} items; first={first}")
        except Exception as exc:  # noqa: BLE001
            _p(f"-- variant {variant}: FAILED {exc}")
    try:
        deep = asx.fetch_list("LBL", 120, session)
        dates = sorted(str(i["date"])[:10] for i in deep)
        _p(f"pagination check: {len(deep)} items, {dates[0]}..{dates[-1]}")
        items = asx.fetch_list("LBL", 5, session)
        _p(f"parsed items: {json.dumps(items, indent=1, default=str)[:2500]}")
        if items:
            key = items[0]["id"]
            for variant in (asx.PDF_GATEWAY.format(key=key),
                            asx.PDF_GATEWAY.format(key=key).split("?")[0]):
                try:
                    resp = get(variant, session=session)
                    _p(f"pdf gateway {variant[:90]}... -> "
                       f"{resp.headers.get('Content-Type')} "
                       f"{len(resp.content)}B magic={resp.content[:5]!r}")
                except Exception as exc:  # noqa: BLE001
                    _p(f"pdf gateway {variant[:90]}... FAILED: {exc}")
            pdf = asx.resolve_pdf_url(items[0], session)
            _p(f"resolve_pdf_url: {pdf}")
    except Exception as exc:  # noqa: BLE001
        _p(f"fetch_list failed: {exc}")


def probe_nsw_coal():
    from .ingest import nsw_coal_exports
    df = nsw_coal_exports.fetch()
    _p(f"nsw_coal_exports: {len(df)} rows "
       f"{df['date'].min().date()}..{df['date'].max().date()} "
       f"last value {df['value'].iloc[-1]:.0f} A$k")


def probe_oem_orders():
    from .ingest import oem_orders
    fls = oem_orders.fls_documents()
    _p(f"FLS: {len(fls)} report PDFs; latest: "
       + "; ".join(f"{d['period_end'].date()} {d['url'][-28:]}" for d in fls[:4]))
    weir = oem_orders.weir_documents()
    _p(f"Weir: {len(weir)} results articles; sample: "
       + "; ".join(d["url"].rsplit("/", 1)[-1][:40] for d in weir[:6]))


PROBES = {
    "abs": probe_abs,
    "nsw_coal": probe_nsw_coal,
    "oem_orders": probe_oem_orders,
    "qld_coal": probe_qld_coal,
    "pilbara": probe_pilbara,
    "rba": probe_rba,
    "aisi": probe_aisi,
    "fred": probe_fred,
    "baker_hughes": probe_baker_hughes,
    "cat_edgar": probe_cat_edgar,
    "jsa": probe_jsa,
    "tungsten": probe_tungsten,
    "asx": probe_asx,
}


def main(names: list[str] | None = None) -> int:
    names = names or list(PROBES)
    failed = []
    for name in names:
        _p(f"\n{LINE}\nPROBE {name}\n{LINE}")
        try:
            PROBES[name]()
            _p(f"PROBE {name}: OK")
        except Exception as exc:  # noqa: BLE001
            failed.append(name)
            _p(f"PROBE {name}: FAILED: {exc}")
            traceback.print_exc()
    _p(f"\n{LINE}\nprobe summary: {len(names) - len(failed)}/{len(names)} ok; "
       f"failed={failed}")
    return 0  # diagnostics always exit 0; the smoke tests are the gate


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or None))
