"""ASX announcement monitor for LBL, EHL, MSV, MAD.

Pulls each ticker's announcement list from ASX's public JSON API, stores
the metadata, then (when OPENAI_API_KEY is set) fetches new
announcement PDFs and classifies them with the OpenAI API:

  EHL  -> Emeco operating utilisation %
  MSV  -> Mitchell Services average operating rigs
  MAD  -> Mader activity commentary
  LBL  -> Technology pipeline events staged
          lead -> trial -> agreement -> cell_ordered -> commissioned -> recurring

Stored:
  events/announcements               metadata per announcement
  events/announcement_extractions    raw JSON extraction per processed PDF
  events/lbl_technology_events       one row per classified LBL Technology event
  observations: emeco.utilisation_pct, mitchell.avg_operating_rigs
"""
from __future__ import annotations

import json
import logging
import time

import pandas as pd

from .. import llm_client
from ..config import cfg
from ..http import SourceFetchError, get, make_session
from ..store import (log_gap, now_utc, read_events, stable_id, write_events,
                     write_observations)

log = logging.getLogger("lbl_tracker.asx")

SOURCE = "asx_announcements"
# Verified live 2026-08-20: the legacy www.asx.com.au/asx/1 API is gone
# (uri-not-found); the Markit Digital research API answers without a token.
# Item shape: {announcementType, date, documentKey, fileSize, headline,
# isPriceSensitive, url("")} - the PDF is served by the Markit file gateway
# keyed on documentKey.
LIST_ENDPOINTS = [
    "https://asx.api.markitdigital.com/asx-research/1.0/companies/{ticker}"
    "/announcements?itemsPerPage={count}&page={page}",
    "https://www.asx.com.au/asx/1/company/{ticker}/announcements?count={count}"
    "&market_sensitive=false&page={page}",
]
PAGE_SIZE = 50  # the Markit API quietly caps large itemsPerPage values
# Verified live 2026-08-20: the file gateway serves the announcement PDF
# with no access token required.
PDF_GATEWAY = ("https://cdn-api.markitdigital.com/apiman-gateway/ASX/asx-research/1.0"
               "/file/{key}")

# Only spend extraction calls on announcements that can carry the fields we
# track. LBL gets everything (Technology events show up under many titles).
RELEVANT = {
    "LBL": None,
    "EHL": ("quarterly", "half year", "full year", "fy2", "results", "operational",
            "utilisation", "utilization", "investor", "trading update", "agm"),
    "MSV": ("quarterly", "half year", "full year", "fy2", "results", "operational",
            "rig", "investor", "trading update", "agm"),
    "MAD": ("quarterly", "half year", "full year", "fy2", "results", "operational",
            "investor", "trading update", "agm"),
}

PROMPTS = {
    "EHL": """This is an ASX announcement PDF from Emeco Holdings (EHL).
Extract ONLY figures explicitly stated in the document. Reply with a single JSON object:
{"operating_utilisation_pct": <number or null>,
 "period": "<period the figure refers to, e.g. 'Q1 FY25', or null>",
 "commentary": "<one-sentence verbatim-faithful summary of equipment demand commentary, or null>"}
If the document states no operating utilisation figure, use null. Never guess.""",
    "MSV": """This is an ASX announcement PDF from Mitchell Services (MSV).
Extract ONLY figures explicitly stated. Reply with a single JSON object:
{"average_operating_rigs": <number or null>,
 "period": "<period the figure refers to, or null>",
 "commentary": "<one-sentence summary of drilling demand commentary, or null>"}
If no average operating rigs figure is stated, use null. Never guess.""",
    "MAD": """This is an ASX announcement PDF from Mader Group (MAD).
Reply with a single JSON object:
{"activity_commentary": "<one-sentence faithful summary of maintenance-labour activity/outlook commentary, or null>",
 "revenue_guidance_aud_m": <number or null>}
Only report figures explicitly stated. Never guess.""",
    "LBL": """This is an ASX announcement PDF from LaserBond (LBL), a surface-engineering company.
Identify any Technology-segment events (licensing of LaserBond cladding technology,
license fees, cladding cell sales to licensees, trials with prospective licensees).
Reply with a single JSON object:
{"technology_events": [
   {"stage": "<one of: lead|trial|agreement|cell_ordered|commissioned|recurring>",
    "counterparty": "<name or null>",
    "value_aud": <total stated dollar value in AUD, number or null>,
    "recognised_aud": <dollar value stated as already recognised, number or null>,
    "description": "<one sentence quoting the announcement's substance>"}
 ],
 "is_technology_related": <true|false>,
 "other_segment_note": "<one sentence if the announcement is Services/Products material, else null>"}
Stages: lead=prospect named, trial=samples/trial work, agreement=license agreement signed,
cell_ordered=licensee ordered a cladding cell, commissioned=cell installed/commissioned,
recurring=ongoing royalties/consumables revenue stated.
Only report events and dollar values explicitly stated in the document. Never guess.
If there are none, return an empty list.""",
}


def fetch_list(ticker: str, count: int, session) -> list[dict]:
    """Page through the announcement list until `count` items (or the feed
    runs dry). The Markit API quietly caps itemsPerPage, so deep backfills
    must paginate."""
    last_err: Exception | None = None
    for template in LIST_ENDPOINTS:
        out, seen, page = [], set(), 1
        while len(out) < count and page <= max(2, count):
            url = template.format(ticker=ticker, count=min(count, PAGE_SIZE), page=page)
            try:
                payload = get(url, session=session).json()
            except (SourceFetchError, ValueError) as exc:
                last_err = exc
                break
            items = payload.get("data") or payload.get("items") or []
            if isinstance(items, dict):
                items = items.get("items", [])
            if not items:
                break
            fresh = [i for i in _parse_items(ticker, items) if i["id"] not in seen]
            if not fresh:  # paging unsupported or exhausted - same page again
                break
            seen.update(i["id"] for i in fresh)
            out.extend(fresh)
            page += 1
            time.sleep(0.2)
        if out:
            log.info("asx %s: %d announcements (%d pages) via %s", ticker, len(out),
                     page - 1, template.split("?")[0])
            return out[:count]
    raise SourceFetchError(f"asx {ticker}: all list endpoints failed; last: {last_err}")


def _parse_items(ticker: str, items: list) -> list[dict]:
    out = []
    for item in items:
        header = (item.get("headline") or item.get("header")
                  or item.get("headerText") or item.get("title") or "")
        doc_key = str(item.get("documentKey") or "")
        doc_url = (item.get("url") or item.get("documentUrl")
                   or item.get("announcementUrl") or "")
        if not doc_url and doc_key:
            doc_url = PDF_GATEWAY.format(key=doc_key)
        date = (item.get("date") or item.get("document_release_date")
                or item.get("releaseDate") or item.get("documentDate") or "")
        ann_id = str(item.get("id") or doc_key
                     or stable_id(ticker, header, str(date)))
        out.append({
            "id": ann_id, "ticker": ticker, "headline": str(header).strip(),
            "date": date, "url": doc_url,
            "market_sensitive": bool(item.get("isPriceSensitive")
                                     or item.get("market_sensitive") or False),
            "type": str(item.get("announcementType") or ""),
            "raw": json.dumps(item)[:4000],
        })
    return out


def resolve_pdf_url(item: dict, session) -> str | None:
    """The list 'url' may be a display page; resolve to the direct PDF."""
    url = item.get("url") or ""
    if not url:
        return None
    if url.lower().endswith(".pdf"):
        return url
    try:
        resp = get(url, session=session)
    except SourceFetchError:
        return None
    if "application/pdf" in resp.headers.get("Content-Type", ""):
        return resp.url
    # display page with a link to the pdf
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(resp.text, "lxml")
    for a in soup.find_all("a", href=True):
        if ".pdf" in a["href"].lower():
            from urllib.parse import urljoin
            return urljoin(resp.url, a["href"])
    return None


def is_relevant(ticker: str, headline: str) -> bool:
    keywords = RELEVANT.get(ticker)
    if keywords is None:
        return True
    low = headline.lower()
    return any(k in low for k in keywords)


def classify_pending(limit: int = 25) -> dict:
    """Extract structured data from announcements not yet processed."""
    announcements = read_events("announcements")
    if announcements.empty:
        return {"processed": 0}
    done = read_events("announcement_extractions")
    done_ids = set(done["id"]) if len(done) else set()
    pending = announcements[~announcements["id"].isin(done_ids)]
    pending = pending[[is_relevant(t, h) for t, h in
                       zip(pending["ticker"], pending["headline"])]]
    pending = pending.sort_values("date", ascending=False).head(limit)
    if pending.empty:
        return {"processed": 0}
    if not llm_client.have_key():
        log_gap(SOURCE, "extractions", f"{len(pending)} announcements pending "
                                       "classification: OPENAI_API_KEY not set")
        return {"processed": 0, "pending": int(len(pending))}

    session = make_session()
    extractions, obs_rows, tech_rows = [], [], []
    retrieved = now_utc()
    for _, item in pending.iterrows():
        pdf_url = resolve_pdf_url(item, session)
        if not pdf_url:
            log_gap(SOURCE, f"{item['ticker']}/{item['id']}", "PDF URL unresolvable")
            continue
        try:
            pdf = get(pdf_url, session=session).content
            result = llm_client.extract_json_from_pdf(pdf, PROMPTS[item["ticker"]])
        except Exception as exc:  # noqa: BLE001
            log_gap(SOURCE, f"{item['ticker']}/{item['id']}", f"extraction failed: {exc}")
            continue
        extractions.append({
            "id": item["id"], "ticker": item["ticker"], "date": item["date"],
            "headline": item["headline"], "pdf_url": pdf_url,
            "extraction": json.dumps(result),
            "model": llm_client.model_name(),
            "retrieved_at": retrieved,
        })
        date = pd.to_datetime(str(item["date"]).split("T")[0], errors="coerce")
        if item["ticker"] == "EHL" and result.get("operating_utilisation_pct") is not None:
            obs_rows.append({"series_id": "emeco.utilisation_pct", "date": date,
                             "value": float(result["operating_utilisation_pct"]),
                             "source_url": pdf_url, "retrieved_at": retrieved})
        if item["ticker"] == "MSV" and result.get("average_operating_rigs") is not None:
            obs_rows.append({"series_id": "mitchell.avg_operating_rigs", "date": date,
                             "value": float(result["average_operating_rigs"]),
                             "source_url": pdf_url, "retrieved_at": retrieved})
        if item["ticker"] == "LBL":
            for event in result.get("technology_events") or []:
                tech_rows.append({
                    "id": stable_id(item["id"], event.get("stage", ""),
                                    event.get("counterparty") or ""),
                    "announcement_id": item["id"], "date": date,
                    "stage": event.get("stage"),
                    "counterparty": event.get("counterparty"),
                    "value_aud": event.get("value_aud"),
                    "recognised_aud": event.get("recognised_aud"),
                    "description": event.get("description"),
                    "source_pdf_url": pdf_url, "retrieved_at": retrieved,
                })
        time.sleep(0.5)

    if extractions:
        write_events("announcement_extractions", pd.DataFrame(extractions), key="id")
    if tech_rows:
        write_events("lbl_technology_events", pd.DataFrame(tech_rows), key="id")
    if obs_rows:
        write_observations(SOURCE, pd.DataFrame(obs_rows))
    return {"processed": len(extractions), "tech_events": len(tech_rows),
            "obs_rows": len(obs_rows)}


def ingest(backfill: bool = False) -> dict:
    session = make_session()
    tickers = cfg("announcements", "tickers", default=["LBL"])
    count = 500 if backfill else cfg("announcements", "lookback_count", default=20)
    retrieved = now_utc()
    all_items, failures = [], []
    for ticker in tickers:
        try:
            all_items.extend(fetch_list(ticker, count, session))
        except SourceFetchError as exc:
            failures.append(str(exc))
            log_gap(SOURCE, ticker, str(exc))
    if not all_items:
        raise RuntimeError(f"asx: all tickers failed: {failures}")
    df = pd.DataFrame(all_items)
    df["retrieved_at"] = retrieved
    stats = write_events("announcements", df, key="id")
    extraction = classify_pending(limit=500 if backfill else 25)
    return {"rows_written": stats["rows_written"], "rows_total": stats["rows_total"],
            "extraction": extraction, "list_failures": failures}
