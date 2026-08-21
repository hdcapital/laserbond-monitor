"""Order-intake signals from LaserBond's two dominant Products customers.

LaserBond's Products segment sells almost entirely to Weir Group and
FLSmidth (2 customers ~= 100% of segment revenue per LBL's economic-
dependency note), so their reported order intake is the most direct
external demand signal available.

Sources (verified live 2026-08-21, see SOURCES.md):
  - FLSmidth: quarterly interim / annual report PDFs listed on
    flsmidth.com/en/investors/financial-downloads (curl_cffi
    impersonation; PDFs on media.ffycdn.net + hugin.info archive).
    Text pages mentioning "order intake" are LLM-extracted to JSON.
  - Weir Group: results / trading-update articles on global.weir,
    discovered via sitemap.xml (the JS-rendered investor pages carry no
    links; the newsroom articles are plain HTML).

Series written (date = period end):
  - fls.order_intake_dkkm          quarterly order intake, DKK million
                                   (mining business; post-2024 demerger
                                   the group IS the mining business)
  - fls.order_intake_growth_pct    organic YoY order-intake growth, %
  - weir.minerals_orders_growth_pct  Weir Minerals division orders
                                   growth YoY, % (constant currency as
                                   reported)

Extraction facts carry the source document URL; documents are processed
once (event log keyed by doc id). Without OPENAI_API_KEY, discovery
still runs but no new extractions are made (no-op, never fabricates).
"""
from __future__ import annotations

import logging
import re
from io import BytesIO

import pandas as pd

from .. import llm_client
from ..http import get_impersonated
from ..store import (log_gap, now_utc, read_events, stable_id, write_events,
                     write_observations)

log = logging.getLogger("lbl_tracker.oem_orders")

SOURCE = "oem_orders"
FLS_DOWNLOADS = "https://www.flsmidth.com/en/investors/financial-downloads"
WEIR_SITEMAP = "https://www.global.weir/sitemap.xml"

FLS_PROMPT = """From this excerpt of an FLSmidth quarterly/annual report, extract the
order intake for the most recent reporting quarter it covers.
FLSmidth reported a Mining and a Cement business until the cement demerger
(2024); afterwards the whole group is the mining business. Prefer the
Mining figure where segments exist, else the group figure.
Return JSON:
{"period_end": "YYYY-MM-DD",           // end of the quarter the figures are for
 "order_intake_dkkm": number|null,     // that quarter's order intake, DKK million
 "organic_growth_pct": number|null,    // organic YoY order-intake growth %, if stated
 "basis": "mining"|"group"|null}
Use null when a figure is genuinely not in the text. Never estimate."""

WEIR_PROMPT = """From this Weir Group results/trading-update article, extract order growth
for the most recent period it reports.
Return JSON:
{"period_end": "YYYY-MM-DD",              // end of the reported period (quarter or half)
 "minerals_orders_yoy_pct": number|null,  // Minerals division orders growth YoY %, as stated
 "group_orders_yoy_pct": number|null}
Growth figures are usually stated like "orders up 4%" (constant currency).
Down x% means negative. Use null when not stated. Never estimate."""


# --- discovery ---------------------------------------------------------------

def fls_documents() -> list[dict]:
    """Quarterly/annual report PDFs with inferred publication date."""
    resp = get_impersonated(FLS_DOWNLOADS)
    html = resp.text
    docs = []
    for m in re.finditer(r'href="(https?://[^"]+\.pdf)"', html):
        before = re.sub(r"<[^>]+>", " ", html[max(0, m.start() - 3000):m.start()])
        before = re.sub(r"\s+", " ", before)
        tail = before[-260:]
        if re.search(r"Presentation", tail, re.I):
            continue
        if not re.search(r"Interim|Quarterly Report|Annual", tail, re.I):
            continue
        dm = re.findall(r"(\d{1,2})/(\d{1,2})/(\d{4})", tail)
        if not dm:
            continue
        mth, day, yr = map(int, dm[-1])
        pub = pd.Timestamp(yr, mth, day)
        q_end = (pub - pd.offsets.QuarterEnd(1)).normalize()
        docs.append({"id": stable_id("fls", m.group(1)), "url": m.group(1),
                     "published": pub, "period_end": q_end})
    seen, out = set(), []
    for d in docs:
        if d["id"] not in seen:
            seen.add(d["id"])
            out.append(d)
    log.info("FLS: %d candidate report PDFs", len(out))
    return out


def weir_documents() -> list[dict]:
    """Results/IMS articles from the Weir sitemap."""
    resp = get_impersonated(WEIR_SITEMAP)
    locs = re.findall(r"<loc>([^<]+)</loc>", resp.text)
    pat = re.compile(r"/newsroom/[^<]*(result|ims|interim|trading[- ]statement|"
                     r"trading[- ]update|half[- ]year)", re.I)
    urls = [u for u in locs if pat.search(u)]
    docs = [{"id": stable_id("weir", u), "url": u} for u in sorted(set(urls))]
    log.info("Weir: %d candidate results articles", len(docs))
    return docs


# --- extraction --------------------------------------------------------------

def _fls_text(pdf_bytes: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(BytesIO(pdf_bytes))
    picked = []
    for page in reader.pages[:30]:
        t = page.extract_text() or ""
        if re.search(r"[Oo]rder intake", t):
            picked.append(t)
        if len(picked) >= 4:
            break
    return "\n\n".join(picked)[:16000]


def _weir_text(html: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text[:16000]


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f


def ingest(backfill: bool = True) -> dict:
    processed = read_events("oem_orders_docs")
    done = set(processed["id"]) if len(processed) else set()

    docs = ([dict(d, company="fls") for d in fls_documents()]
            + [dict(d, company="weir") for d in weir_documents()])
    todo = [d for d in docs if d["id"] not in done]
    if not backfill:
        todo = todo[:8]
    if todo and not llm_client.have_key():
        log_gap(SOURCE, "fls.order_intake_dkkm",
                f"{len(todo)} unprocessed documents but OPENAI_API_KEY not set")
        return {"source": SOURCE, "status": "ok", "rows": 0,
                "note": "no key; discovery only"}

    obs_rows, doc_rows, errors = [], [], 0
    for d in todo:
        try:
            if d["company"] == "fls":
                pdf = get_impersonated(d["url"]).content
                text = _fls_text(pdf)
                if not text.strip():
                    doc_rows.append({**d, "status": "no order-intake text"})
                    continue
                # no low max_tokens cap: reasoning models spend completion
                # tokens on reasoning first and would return empty content
                fact = llm_client.extract_json_from_text(text, FLS_PROMPT)
                period = pd.Timestamp(fact.get("period_end"))
                if pd.isna(period):
                    raise ValueError(f"bad period_end {fact.get('period_end')!r}")
                level = _num(fact.get("order_intake_dkkm"))
                growth = _num(fact.get("organic_growth_pct"))
                if level is not None and not (100 <= level <= 60000):
                    raise ValueError(f"FLS order intake implausible: {level}")
                for sid, val in [("fls.order_intake_dkkm", level),
                                 ("fls.order_intake_growth_pct", growth)]:
                    obs_rows.append({"series_id": sid, "date": period,
                                     "value": val, "source_url": d["url"],
                                     "retrieved_at": now_utc()})
            else:
                html = get_impersonated(d["url"]).text
                fact = llm_client.extract_json_from_text(_weir_text(html),
                                                         WEIR_PROMPT)
                period = pd.Timestamp(fact.get("period_end"))
                if pd.isna(period):
                    raise ValueError(f"bad period_end {fact.get('period_end')!r}")
                growth = _num(fact.get("minerals_orders_yoy_pct"))
                if growth is not None and abs(growth) > 80:
                    raise ValueError(f"Weir orders growth implausible: {growth}")
                obs_rows.append({"series_id": "weir.minerals_orders_growth_pct",
                                 "date": period, "value": growth,
                                 "source_url": d["url"],
                                 "retrieved_at": now_utc()})
            doc_rows.append({**d, "status": "ok", "fact": str(fact)})
        except Exception as exc:  # noqa: BLE001 - one bad doc must not kill the run
            errors += 1
            log.warning("oem_orders: %s failed: %s", d["url"], exc)
            log_gap(SOURCE, f"{d['company']}.orders", f"{d['url']}: {exc}")

    stats = {"source": SOURCE, "status": "ok", "rows": 0,
             "docs_processed": len(doc_rows), "errors": errors}
    if obs_rows:
        frame = pd.DataFrame(obs_rows)
        frame["date"] = pd.to_datetime(frame["date"])
        res = write_observations(SOURCE, frame)
        stats["rows"] = res["rows_written"]
    if doc_rows:
        for dr in doc_rows:
            dr.pop("period_end", None)
            dr["published"] = str(dr.get("published", ""))
        write_events("oem_orders_docs", pd.DataFrame(doc_rows), key="id")
    return stats
