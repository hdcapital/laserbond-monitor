"""One-off research probe round 8 (runs in CI where the network is open).

Goal: raw ORDER-INTAKE text from LaserBond's two dominant Products
customers, so an extraction gate can be designed:

  1. Weir (Investegate RNS archive): list results/IMS announcements
     2019->, fetch a few, print the sentences mentioning orders.
  2. FLSmidth: map interim-report PDFs to quarters (title text search),
     download 3 samples, print the "order intake" table text.

Prints machine-parseable blocks to stdout; nothing is written to the store.
Delete this script once the research round is done.
"""
from __future__ import annotations

import re
import sys


def block(name: str, text: str) -> None:
    print(f"\n===BEGIN {name}===")
    print(text)
    print(f"===END {name}===")


def creq_get(url: str):
    from curl_cffi import requests as creq
    return creq.get(url, impersonate="chrome", timeout=60)


RESULT_KINDS = ("half-year-financial-report", "final-results", "annual-financial-report",
                "trading-statement", "trading-update", "1st-quarter-results",
                "3rd-quarter-results", "interim-management-statement", "q1", "q3")


def weir_announcements() -> list[tuple[str, str]]:
    """Collect (title, url) result-ish announcements from Investegate."""
    found = []
    # company page shows recent; archive pages by year via /company/WEIR?page=N
    for page in range(0, 14):
        url = f"https://www.investegate.co.uk/company/WEIR?page={page}"
        try:
            r = creq_get(url)
            links = re.findall(r'href="(/announcement/rns/[^"]+)"[^>]*>([^<]{0,140})',
                               r.text)
            for h, t in links:
                if any(k in h for k in RESULT_KINDS):
                    found.append((t.strip(), "https://www.investegate.co.uk" + h))
        except Exception as exc:  # noqa: BLE001
            block(f"WEIR LIST page{page} ERROR", repr(exc))
            break
    seen, uniq = set(), []
    for t, u in found:
        if u not in seen:
            seen.add(u)
            uniq.append((t, u))
    block("WEIR RESULT ANNS", "\n".join(f"{t} | {u}" for t, u in uniq[:60]))
    return uniq


def weir_order_text(anns: list[tuple[str, str]]) -> None:
    for t, u in anns[:4]:
        try:
            r = creq_get(u)
            text = re.sub(r"<[^>]+>", " ", r.text)
            text = re.sub(r"\s+", " ", text)
            hits = re.findall(r"[^.]{0,180}[Oo]rders?[^.]{0,180}\.", text)
            mins = [h for h in hits if re.search(r"[Mm]inerals|[Dd]ivision|%", h)]
            block(f"WEIR TEXT {t[:40]} {u[-9:]}",
                  "\n---\n".join((mins or hits)[:12]))
        except Exception as exc:  # noqa: BLE001
            block(f"WEIR TEXT {u[-9:]} ERROR", repr(exc))


def fls_quarter_map() -> list[tuple[str, str]]:
    r = creq_get("https://www.flsmidth.com/en/investors/financial-downloads")
    html = r.text
    rows = []
    for m in re.finditer(r'href="(https?://[^"]+\.pdf)"', html):
        before = re.sub(r"<[^>]+>", " ", html[max(0, m.start() - 3000):m.start()])
        before = re.sub(r"\s+", " ", before)
        qm = re.findall(r"((?:Q[1-4]|H[12]|Annual|Interim)[^|]{0,60}?(?:19|20)\d\d"
                        r"|(?:19|20)\d\d[^|]{0,30}?(?:Q[1-4]|H[12]|Interim|Annual)[^ ]*)",
                        before)
        rows.append((qm[-1].strip() if qm else "?", m.group(1)))
    block("FLS QUARTER MAP", "\n".join(f"{q} | {u}" for q, u in rows[:90]))
    return rows


def fls_order_text(rows: list[tuple[str, str]]) -> None:
    from io import BytesIO

    from pypdf import PdfReader
    interim = [(q, u) for q, u in rows if re.search(r"Q[1-4]|Interim", q, re.I)]
    for q, u in interim[:3]:
        try:
            r = creq_get(u)
            reader = PdfReader(BytesIO(r.content))
            snippets = []
            for i, page in enumerate(reader.pages[:12]):
                t = page.extract_text() or ""
                if re.search(r"[Oo]rder intake", t):
                    lines = [ln for ln in t.splitlines()
                             if re.search(r"[Oo]rder intake|Mining|MAK", ln)]
                    snippets.append(f"--- page {i}:\n" + "\n".join(lines[:20]))
                if len(snippets) >= 3:
                    break
            block(f"FLS TEXT {q[:40]}", f"{u}\npages={len(reader.pages)}\n"
                  + "\n".join(snippets))
        except Exception as exc:  # noqa: BLE001
            block(f"FLS TEXT {q[:30]} ERROR", repr(exc))


def main() -> int:
    anns = weir_announcements()
    weir_order_text(anns)
    rows = fls_quarter_map()
    fls_order_text(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
