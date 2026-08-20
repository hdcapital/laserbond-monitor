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
    for flow in ("CAPEX", "MERALS_EXP"):
        try:
            df = abs_sdmx.get_data(flow, "all", params={"startPeriod": "2023"})
            _p(f"-- {flow}: {len(df)} obs since 2023; columns={list(df.columns)}")
            for col in df.columns:
                if col.endswith("_name"):
                    _p(f"   {col}: {sorted(map(str, df[col].dropna().unique()))[:25]}")
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
    _p(f"discovered saleable resources: {json.dumps(found, indent=1)[:3000]}")
    if found:
        res = found[0]
        content = qld_coal._download(res["url"], session).content
        _p(f"first resource bytes: {len(content)}")
        if res["format"] == "csv":
            _p(_head(content.decode("utf-8", "replace"), 2500))
        else:
            book = pd.read_excel(io.BytesIO(content), sheet_name=None, header=None)
            for sheet, raw in list(book.items())[:4]:
                _p(f"  sheet {sheet!r} shape={raw.shape}")
                _p(raw.head(12).to_string()[:2500])


def probe_pilbara():
    from bs4 import BeautifulSoup

    from .ingest import pilbara_ports
    session = make_session()
    url = pilbara_ports.discover_listing(session)
    _p(f"listing resolved: {url}")
    for probe_url in (url, f"{url}?page=2"):
        soup = BeautifulSoup(get(probe_url, session=session).text, "lxml")
        _p(f"-- anchors on {probe_url}:")
        for a in soup.find_all("a", href=True)[:150]:
            text = " ".join(a.get_text(" ", strip=True).split())
            if text:
                _p(f"   {text[:110]} -> {a['href'][:130]}")
    items = pilbara_ports.list_statements(session)
    _p(f"candidates: {len(items)}")
    for item in items[:15]:
        _p(f"- {item['title']} -> {item['url']}")
    if items:
        text = BeautifulSoup(get(items[0]["url"], session=session).text,
                             "lxml").get_text(" ", strip=True)
        _p("first statement text excerpt:")
        _p(_head(" ".join(text.split()), 3500))


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
    links = baker_hughes.discover_links(session)
    _p(f"resolved: {links}")
    for kind, url in links.items():
        if not url:
            continue
        content = get(url, session=session).content
        book = pd.read_excel(io.BytesIO(content), sheet_name=None, header=None)
        _p(f"-- {kind} workbook sheets: {list(book)}")
        for sheet, raw in list(book.items())[:3]:
            _p(f"   sheet {sheet!r} shape={raw.shape}")
            _p(raw.head(10).to_string()[:2000])


def probe_cat_edgar():
    from .ingest import cat_edgar
    session = make_session(sec=True)
    hits = cat_edgar.search_filings(session)
    _p(f"FTS hits: {len(hits)}")
    for hit in hits[:10]:
        _p(f"- {hit}")
    if hits:
        url = cat_edgar.exhibit_url(hits[0])
        _p(f"first exhibit: {url}")
        html = get(url, session=session, sec=True).text
        from bs4 import BeautifulSoup
        text = " ".join(BeautifulSoup(html, "lxml").get_text(" ", strip=True).split())
        _p(_head(text, 5000))
        try:
            tables = pd.read_html(io.StringIO(html))
            _p(f"tables: {len(tables)}")
            for i, table in enumerate(tables[:6]):
                _p(f"-- table {i} shape={table.shape}")
                _p(table.head(8).to_string()[:1800])
        except ValueError as exc:
            _p(f"read_html: {exc}")


def probe_jsa():
    from .ingest import jsa_ivi
    session = jsa_ivi.jsa_session()
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
        content = get(url, session=session).content
        book = pd.read_excel(io.BytesIO(content), sheet_name=None, header=None)
        _p(f"sheets: {list(book)}")
        for sheet, raw in list(book.items())[:3]:
            _p(f"-- sheet {sheet!r} shape={raw.shape}")
            _p(raw.head(8).iloc[:, :12].to_string()[:2000])
    except Exception as exc:  # noqa: BLE001
        _p(f"workbook discovery failed: {exc}")


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
            url = template.format(ticker=ticker, count=5)
            try:
                resp = get(url, session=session)
                _p(f"-- {url}")
                _p(_head(resp.text, 2500))
            except Exception as exc:  # noqa: BLE001
                _p(f"-- {url} FAILED: {exc}")
    try:
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


PROBES = {
    "abs": probe_abs,
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
