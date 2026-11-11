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
from ..http import get, get_impersonated, make_session
from ..store import (log_gap, now_utc, purge_observations, read_events, read_series,
                     run_once, stable_id, write_events, write_observations)

log = logging.getLogger("lbl_tracker.oem_orders")

SOURCE = "oem_orders"
FLS_DOWNLOADS = "https://www.flsmidth.com/en/investors/financial-downloads"
WEIR_SITEMAP = "https://www.global.weir/sitemap.xml"

FLS_PROMPT = """From this excerpt of an FLSmidth quarterly/annual report, extract the
order intake for the most recent reporting QUARTER it covers (three months -
never a full-year, half-year or year-to-date total, and never a single
product line or sub-segment).
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


# Results / IMS / trading-update articles only. The old filter's bare
# "result" and "half-year" also caught a product release ("...half-year
# release improves...") and a customer case study ("...results in
# significant savings..."), which were then sent to the model.
WEIR_RESULTS_URL = re.compile(
    r"/newsroom/.*("
    r"(full|half)[- ]year[- ]results|interim[- ]results|financial[- ]results|"
    r"results[- ](for|\d{4})|\d{4}[- ]results|"
    r"interim[- ]management[- ]statement|(^|[-/])ims([-/]|$)|"
    r"trading[- ](update|statement))", re.I)


def weir_documents() -> list[dict]:
    """Results/IMS articles from the Weir sitemap."""
    resp = get_impersonated(WEIR_SITEMAP)
    locs = re.findall(r"<loc>([^<]+)</loc>", resp.text)
    urls = [u for u in locs if WEIR_RESULTS_URL.search(u)]
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
    """global.weir article bodies live in a JSON-escaped HTML blob inside a
    <script> tag (the visible DOM is a ~50-char stub). Decode the \\uXXXX
    escapes FIRST, then strip tags, then keep windows around order/results
    mentions so surrounding JS noise doesn't drown the content."""
    decoded = re.sub(
        r"\\u([0-9a-fA-F]{4})",
        lambda m: chr(int(m.group(1), 16)),
        html)
    for esc, ch in (("\\n", " "), ("\\r", " "), ("\\t", " "), ('\\"', '"')):
        decoded = decoded.replace(esc, ch)
    text = re.sub(r"<[^>]+>", " ", decoded)
    text = re.sub(r"\s+", " ", text)
    marks = [m.start() for m in
             re.finditer(r"\b[Oo]rders?\b|[Oo]rder intake|Minerals|constant currency",
                         text)]
    if not marks:
        return text[:16000]
    windows, lo_prev, hi_prev = [], -1, -1
    for i in marks:
        lo, hi = max(0, i - 2500), min(len(text), i + 2500)
        if lo <= hi_prev:
            hi_prev = max(hi_prev, hi)
        else:
            if hi_prev > 0:
                windows.append(text[lo_prev:hi_prev])
            lo_prev, hi_prev = lo, hi
    windows.append(text[lo_prev:hi_prev])
    return " [...] ".join(windows)[:20000]


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f


MIN_TEXT_CHARS = 800  # a real report/article; JS-rendered stubs are ~50 chars


class ExtractionRejected(ValueError):
    """The model's answer failed validation against the source document.
    Permanent for that document: it is recorded and never re-sent (re-asking
    until some number happens to pass would defeat the check)."""


_NEG_CUES = re.compile(r"\b(down|decreas\w*|declin\w*|lower|fell|fall(?:en|ing)?|"
                       r"drop(?:ped)?|reduc\w*|contract\w*|minus|negative)\b", re.I)
_POS_CUES = re.compile(r"\b(up|increas\w*|grew|grow(?:th|n|ing)?|ris(?:e|en|ing)|rose|"
                       r"higher|gain\w*|plus)\b", re.I)


def _number_forms(v: float, pct: bool) -> set[str]:
    forms = {f"{v:g}", f"{v:.1f}"}
    if v == int(v):
        forms |= {str(int(v)), f"{int(v):,}"}
    return {re.escape(f) for f in forms}


_NEG_AFTER = re.compile(r"\s*(lower|down|decrease\w*|declin\w*|less|below|reduction|"
                        r"drop|fall)\b", re.I)
_POS_AFTER = re.compile(r"\s*(higher|up|increase\w*|growth|rise|above|more)\b", re.I)


def _sign_at(text: str, start: int, end: int | None = None) -> int:
    """Sign implied by the words around a figure: an explicit minus; else
    the word right after it ("2.5% lower"); else the NEAREST up/down cue in
    the preceding 60 chars ("down 3%"); else +."""
    if start > 0 and text[start - 1] in "-\u2212":
        return -1
    if end is not None:
        if _NEG_AFTER.match(text, end):
            return -1
        if _POS_AFTER.match(text, end):
            return 1
    before = text[max(0, start - 60):start]
    neg = [m.end() for m in _NEG_CUES.finditer(before)]
    posi = [m.end() for m in _POS_CUES.finditer(before)]
    if neg and (not posi or max(neg) > max(posi)):
        return -1
    return 1


def _grounded(value, text: str, pct: bool = False, near: tuple[str, ...] = (),
              window: int = 200) -> bool:
    """A number the model 'extracted' must appear in the source text as a
    whole number (not inside a bigger one: "5%" never matches "15%" or
    "2.5%"), within `window` chars of every `near` keyword (e.g. "order"),
    and - for percentages - with the stated sign ("down 3%" is -3, never
    +3). Anything else came from model memory or the wrong line, which is
    fabrication. None (not stated) is always fine."""
    if value is None:
        return True
    v = float(value)
    mag = abs(v)
    unit = r"\s?(?:%|per\s?cent|percent)" if pct else r"(?![\d]|[.,]\d)"
    pattern = re.compile(r"(?<![\d.,])(?:" + "|".join(sorted(_number_forms(mag, pct)))
                         + r")" + unit, re.I)
    near_res = [re.compile(k, re.I) for k in near]
    for m in pattern.finditer(text):
        ctx = text[max(0, m.start() - window):m.end() + window]
        if not all(r.search(ctx) for r in near_res):
            continue
        if pct and v != 0 and _sign_at(text, m.start(), m.end()) != (1 if v > 0 else -1):
            continue
        return True
    return False


def _validate_fls(fact: dict, text: str, doc: dict) -> tuple[pd.Timestamp, float | None,
                                                               float | None]:
    period = pd.Timestamp(fact.get("period_end"))
    if pd.isna(period):
        raise ExtractionRejected(f"bad period_end {fact.get('period_end')!r}")
    expected = doc.get("period_end")
    if expected is not None and pd.Timestamp(expected).to_period("Q") != period.to_period("Q"):
        raise ExtractionRejected(
            f"period_end {period.date()} is not the quarter this report was "
            f"published for ({pd.Timestamp(expected).date()})")
    basis = fact.get("basis")
    level = _num(fact.get("order_intake_dkkm"))
    growth = _num(fact.get("organic_growth_pct"))
    if (level is not None or growth is not None) and period < pd.Timestamp("2024-01-01") \
            and basis != "mining":
        raise ExtractionRejected(f"pre-demerger figure on basis {basis!r}; the series "
                                 f"is the Mining business only")
    if level is not None and not (800 <= level <= 15000):
        raise ExtractionRejected(f"quarterly order intake implausible: {level}")
    if not _grounded(level, text, near=(r"order intake",)):
        raise ExtractionRejected(f"order intake {level} not stated near 'order intake'")
    if not _grounded(growth, text, pct=True, near=(r"order",)):
        raise ExtractionRejected(f"growth {growth} not stated (with that sign) near 'order'")
    return period.normalize(), level, growth


def _validate_weir(fact: dict, text: str) -> tuple[pd.Timestamp, dict]:
    period = pd.Timestamp(fact.get("period_end"))
    if pd.isna(period):
        raise ExtractionRejected(f"bad period_end {fact.get('period_end')!r}")
    values = {}
    for field, sid, near in [
        ("minerals_orders_yoy_pct", "weir.minerals_orders_growth_pct",
         (r"\bminerals\b", r"\border")),
        ("group_orders_yoy_pct", "weir.group_orders_growth_pct", (r"\border",)),
    ]:
        growth = _num(fact.get(field))
        if growth is not None and abs(growth) > 80:
            raise ExtractionRejected(f"{field} implausible: {growth}")
        if not _grounded(growth, text, pct=True, near=near):
            raise ExtractionRejected(f"{field} {growth} not stated (with that sign) "
                                     f"near {near}")
        values[sid] = growth
    return period.normalize(), values


def _rebuild_combined_weir() -> None:
    """weir.orders_growth_pct = Minerals-division growth where stated,
    else the group figure. Pure recombination of grounded observations -
    each point keeps the source URL of the figure it came from."""
    m = read_series("weir.minerals_orders_growth_pct").dropna(subset=["value"])
    g = read_series("weir.group_orders_growth_pct").dropna(subset=["value"])
    dates = sorted(set(pd.to_datetime(m["date"])) | set(pd.to_datetime(g["date"])))
    # a combined point whose underlying figure was purged must go too
    stale = [d for d in pd.to_datetime(read_series("weir.orders_growth_pct")["date"])
             if d not in set(dates)]
    if stale:
        purge_observations("oem_orders_combined", "weir.orders_growth_pct",
                           "underlying Minerals/group figure was purged", dates=stale)
    if not dates:
        return
    m = m.assign(date=pd.to_datetime(m["date"])).set_index("date")
    g = g.assign(date=pd.to_datetime(g["date"])).set_index("date")
    rows = []
    for dt in dates:
        src = m if dt in m.index else g
        rows.append({"series_id": "weir.orders_growth_pct", "date": dt,
                     "value": float(src.loc[dt, "value"]),
                     "source_url": str(src.loc[dt, "source_url"]),
                     "retrieved_at": now_utc()})
    write_observations("oem_orders_combined", pd.DataFrame(rows))


FLS_SERIES = ("fls.order_intake_dkkm", "fls.order_intake_growth_pct")
WEIR_SERIES = ("weir.minerals_orders_growth_pct", "weir.group_orders_growth_pct")


def _purge_fls_v1() -> dict:
    """The 2008-2019 FLS history extracted under the old checks mixed bases
    (mining vs group), a full-year figure (2015: 18,490), sub-segment
    fragments (443, 525, ...) and conflicting values for the same quarter
    from different documents. It is unweighted, but it is not data."""
    reason = ("extracted under pre-2026-11 checks: mixed mining/group basis, "
              "full-year and fragment figures, conflicting same-quarter values")
    return {sid: purge_observations(SOURCE, sid, reason) for sid in FLS_SERIES}


def _purge_weir_nonresults_v1() -> dict:
    """Rows extracted from pages that are not results/IMS articles (a
    product release and a customer case study matched the old URL filter)."""
    out = {}
    obs = read_series(WEIR_SERIES[0])
    obs = pd.concat([obs, read_series(WEIR_SERIES[1])])
    bad = obs[~obs["source_url"].astype(str).str.contains(WEIR_RESULTS_URL)]
    for sid, grp in bad.groupby("series_id"):
        out[sid] = purge_observations(SOURCE, sid, "source page is not a Weir "
                                      "results/IMS article", dates=list(grp["date"]))
    return out


def revalidate_weir(limit: int = 60) -> dict:
    """Re-check every STORED Weir figure against its source article with the
    strict grounding rule (whole number, near 'order' [+ 'Minerals'], right
    sign). Uses the plain HTTP client. A figure that fails is purged with an
    audit entry; an article that cannot be fetched is left for the next run.
    No model call is made - this only verifies numbers already stored."""
    docs = read_events("oem_orders_docs")
    if docs.empty:
        return {"checked": 0}
    if "validated" not in docs.columns:
        docs["validated"] = None
    todo = docs[(docs["company"] == "weir") & (docs["status"] == "ok")
                & ~docs["validated"].isin(["v2-ok", "v2-purged"])].head(limit)
    stored = pd.concat([read_series(sid) for sid in WEIR_SERIES])
    stored = stored.dropna(subset=["value"])
    session = make_session()
    updates, checked, purged, unreachable = [], 0, 0, 0
    for _, doc in todo.iterrows():
        rows = stored[stored["source_url"] == doc["url"]]
        if rows.empty:
            updates.append({**doc.to_dict(), "validated": "v2-ok"})
            continue
        try:
            text = _weir_text(get(doc["url"], session=session).text)
        except Exception as exc:  # noqa: BLE001 - retry next run
            unreachable += 1
            log.warning("weir revalidation: %s unreachable: %s", doc["url"], exc)
            continue
        if len(text) < MIN_TEXT_CHARS:
            unreachable += 1
            log.warning("weir revalidation: %s returned no article text", doc["url"])
            continue
        outcome = "v2-ok"
        for _, row in rows.iterrows():
            checked += 1
            near = ((r"\bminerals\b", r"\border") if "minerals" in row["series_id"]
                    else (r"\border",))
            if not _grounded(row["value"], text, pct=True, near=near):
                purge_observations(SOURCE, row["series_id"],
                                   f"failed strict re-validation against {doc['url']} "
                                   f"(value/sign not stated near {near})",
                                   dates=[row["date"]])
                purged += 1
                outcome = "v2-purged"
        updates.append({**doc.to_dict(), "validated": outcome})
    if updates:
        write_events("oem_orders_docs", pd.DataFrame(updates), key="id")
    if unreachable:
        log_gap(SOURCE, "weir.orders", f"revalidation pending for {unreachable} "
                "article(s): not reachable with the plain client this run")
    return {"checked": checked, "purged": purged, "unreachable": unreachable}


def _conflicts(sid: str, period: pd.Timestamp, value, url: str, pending: list) -> bool:
    """True if another document already gave a DIFFERENT figure for the same
    series and quarter - two sources disagreeing means neither is trusted."""
    if value is None:
        return False
    prior = read_series(sid).dropna(subset=["value"])
    prior = prior[(pd.to_datetime(prior["date"]).dt.to_period("Q") == period.to_period("Q"))
                  & (prior["source_url"] != url)]
    others = [r for r in pending if r["series_id"] == sid and r["value"] is not None
              and pd.Timestamp(r["date"]).to_period("Q") == period.to_period("Q")
              and r["source_url"] != url]
    return any(abs(float(v) - float(value)) > 1e-9
               for v in list(prior["value"]) + [r["value"] for r in others])


def ingest(backfill: bool = True) -> dict:
    run_once("2026-11-purge-fls-v1", _purge_fls_v1)
    run_once("2026-11-purge-weir-nonresults-v1", _purge_weir_nonresults_v1)
    reval = revalidate_weir()

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
        _rebuild_combined_weir()
        return {"source": SOURCE, "rows_written": 0, "revalidation": reval,
                "note": "no key; discovery only"}

    obs_rows, doc_rows, errors, rejected = [], [], 0, 0
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
                period, level, growth = _validate_fls(fact, text, d)
                values = {"fls.order_intake_dkkm": level,
                          "fls.order_intake_growth_pct": growth}
            else:
                text = _weir_text(get_impersonated(d["url"]).text)
                if len(text) < MIN_TEXT_CHARS:
                    # JS-rendered stub: a model given only a title would
                    # answer from memory, which is fabrication - never call it
                    doc_rows.append({**d, "status": "stub page - no text"})
                    continue
                fact = llm_client.extract_json_from_text(text, WEIR_PROMPT)
                period, values = _validate_weir(fact, text)
            clashes = [sid for sid, val in values.items()
                       if _conflicts(sid, period, val, d["url"], obs_rows)]
            if clashes:
                raise ExtractionRejected(f"{clashes} for {period.date()} conflict with "
                                         f"a figure already stored from another document")
            for sid, val in values.items():
                obs_rows.append({"series_id": sid, "date": period, "value": val,
                                 "source_url": d["url"], "retrieved_at": now_utc()})
            doc_rows.append({**d, "status": "ok", "fact": str(fact), "validated": "v2-ok"})
        except ExtractionRejected as exc:
            # permanent: never re-ask the model about this document
            rejected += 1
            log.warning("oem_orders: %s rejected: %s", d["url"], exc)
            log_gap(SOURCE, f"{d['company']}.orders", f"{d['url']}: REJECTED {exc}")
            doc_rows.append({**d, "status": f"rejected: {exc}"[:300]})
        except Exception as exc:  # noqa: BLE001 - transient: retried next run
            errors += 1
            log.warning("oem_orders: %s failed: %s", d["url"], exc)
            log_gap(SOURCE, f"{d['company']}.orders", f"{d['url']}: {exc}")

    stats = {"source": SOURCE, "rows_written": 0, "docs_processed": len(doc_rows),
             "rejected": rejected, "errors": errors, "revalidation": reval}
    if obs_rows:
        frame = pd.DataFrame(obs_rows)
        frame["date"] = pd.to_datetime(frame["date"])
        stats["rows_written"] = write_observations(SOURCE, frame)["rows_written"]
    _rebuild_combined_weir()
    if doc_rows:
        for dr in doc_rows:
            dr.pop("period_end", None)
            dr["published"] = str(dr.get("published", ""))
        write_events("oem_orders_docs", pd.DataFrame(doc_rows), key="id")
    return stats
