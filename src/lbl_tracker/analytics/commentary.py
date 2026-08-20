"""AI commentary boxes for the dashboard and email brief.

Each pulse (and the Technology pipeline) gets a 2-3 sentence plain-English
summary written by the OpenAI model from ONLY the computed figures:
weights, latest value, z-scores, per-component attribution and trend
deltas. Commentary is interpretation of data the pipeline already
produced - it is clearly labelled, never stored as an observation and
never feeds back into any score.

Regeneration is fact-hashed: a summary is rewritten only when the numbers
under it change, so scheduled runs cost nothing while data is unchanged.
Output: docs/data/commentary.json.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone

from .. import llm_client
from ..config import REPO_ROOT, cfg

log = logging.getLogger("lbl_tracker.commentary")

OUT_PATH = REPO_ROOT / "docs" / "data" / "commentary.json"

PULSE_PROMPT = """You write one short caption for a financial monitoring dashboard card.

Below are the ONLY facts you may use - the computed state of one composite
indicator for LaserBond (ASX:LBL), a surface-engineering company. Write 2-3
plain-English sentences covering: (1) in one clause, what this composite
tracks and how (weighted z-scores of the listed inputs vs their own trailing
5-year norms, scaled to -100..+100); (2) what the latest reading says relative
to that 5-year norm; (3) the recent trend and which inputs are driving it,
naming the biggest positive and negative contributors. Mention missing inputs
only if they matter. STRICT RULES: use only numbers given here; no forecasts,
no investment advice, no speculation about causes beyond what the inputs show.
Reply with the sentences only - no headings, no markdown.

FACTS:
{facts}"""

TECH_PROMPT = """You write one short caption for a financial monitoring dashboard card.

Below are the ONLY facts you may use - the current state of LaserBond's
Technology-segment licensing pipeline, built from classified ASX announcement
extractions. It is an event table displayed as facts, never scored. Write 2
plain-English sentences: what the pipeline currently holds (stage counts,
stated dollar values) and what changed over the last 6 months, referencing the
named events where helpful. STRICT RULES: use only the facts given; no
forecasts, no investment advice. Reply with the sentences only.

FACTS:
{facts}"""


def _round(value, nd=1):
    try:
        return round(float(value), nd)
    except (TypeError, ValueError):
        return None


def _trend(history: list) -> dict:
    vals = [(h["month"], h["value"]) for h in history if h.get("value") is not None]
    if not vals:
        return {}
    out = {"latest": {"month": vals[-1][0], "value": _round(vals[-1][1])}}
    for label, back in (("1m_ago", 2), ("3m_ago", 4), ("6m_ago", 7), ("12m_ago", 13)):
        if len(vals) >= back:
            out[label] = {"month": vals[-back][0], "value": _round(vals[-back][1])}
    return out


def _pulse_facts(name: str, pulse: dict, attribution: list) -> dict:
    from ..dashboard.build import DISCONTINUED
    from ..store import read_series
    spec = cfg("pulses", name, default={})
    inputs = []
    for comp in attribution:
        if comp.get("status") == "NO DATA":
            sid = comp.get("series")
            if sid in DISCONTINUED:
                status = "source discontinued upstream; excluded from the score"
            else:
                try:
                    s = read_series(sid).dropna(subset=["value"])
                except Exception:  # noqa: BLE001
                    s = None
                comp_spec = next((c for c in spec.get("components", [])
                                  if c.get("series") == sid), {})
                if s is not None and len(s):
                    import pandas as pd
                    last = s["date"].max()
                    months_old = (pd.Timestamp.utcnow().tz_localize(None)
                                  - last).days / 30.44
                    cap = comp_spec.get("max_stale_months")
                    if cap is not None and months_old > cap:
                        status = (f"latest data ({last.date()}) is older than the "
                                  f"{cap}-month staleness cap; excluded from the "
                                  f"score until the source updates")
                    else:
                        status = (f"has current data (latest {last.date()}) but "
                                  f"not yet enough history for a 5-year z-score; "
                                  f"excluded from the score until history "
                                  f"accumulates")
                else:
                    status = "no data retrieved yet; excluded from the score"
            inputs.append({"input": comp.get("label"), "weight": comp.get("weight"),
                           "status": status})
        else:
            inputs.append({
                "input": comp.get("label"),
                "weight": comp.get("weight"),
                "latest_source_value": _round(comp.get("value"), 2),
                "z_score_vs_5yr": _round(comp.get("z"), 2),
                "contribution_to_pulse": _round(comp.get("contribution")),
                "inverted": comp.get("inverted", False),
                "as_of": comp.get("as_of"),
            })
    return {
        "pulse": spec.get("title", name),
        "scale": "-100 (far below 5yr norm) .. +100 (far above 5yr norm)",
        "latest_value": pulse.get("latest_value"),
        "latest_month": pulse.get("latest_month"),
        "trend": _trend(pulse.get("history", [])),
        "inputs": inputs,
    }


def _tech_facts(tech: dict) -> dict:
    return {
        "stage_counts": tech.get("stage_counts"),
        "stage_deltas_last_6_months": tech.get("stage_deltas_6m"),
        "contracted_value_where_stated_aud": tech.get("contracted_value_aud_where_stated"),
        "recognised_where_stated_aud": tech.get("recognised_aud_where_stated"),
        "contracted_unrecognised_where_stated_aud":
            tech.get("contracted_unrecognised_aud_where_stated"),
        "recent_events": [
            {"date": e.get("date"), "stage": e.get("stage"),
             "counterparty": e.get("counterparty"),
             "stated_value_aud": e.get("value_aud"),
             "description": e.get("description")}
            for e in (tech.get("events") or [])[:6]
        ],
    }


def _facts_hash(facts: dict) -> str:
    return hashlib.sha1(json.dumps(facts, sort_keys=True, default=str)
                        .encode()).hexdigest()[:16]


def generate() -> dict:
    pulses_path = REPO_ROOT / "docs" / "data" / "pulses.json"
    if not pulses_path.exists():
        raise RuntimeError("commentary: run `lbl-tracker pulses` first")
    data = json.loads(pulses_path.read_text())
    existing = json.loads(OUT_PATH.read_text()) if OUT_PATH.exists() else {}

    if not llm_client.have_key():
        log.warning("commentary: OPENAI_API_KEY not set - keeping existing "
                    "commentary untouched")
        return existing

    out, generated = dict(existing), 0
    targets = []
    for name, pulse in data.get("pulses", {}).items():
        facts = _pulse_facts(name, pulse, data.get("attribution", {}).get(name, []))
        targets.append((name, PULSE_PROMPT, facts))
    tech = data.get("technology", {})
    if tech.get("available"):
        targets.append(("technology", TECH_PROMPT, _tech_facts(tech)))

    for key, prompt, facts in targets:
        digest = _facts_hash(facts)
        if existing.get(key, {}).get("facts_hash") == digest:
            continue
        try:
            text = llm_client.complete(
                prompt.format(facts=json.dumps(facts, indent=1, default=str)))
        except Exception as exc:  # noqa: BLE001 - stale commentary beats a dead build
            log.warning("commentary: %s generation failed: %s", key, exc)
            continue
        out[key] = {
            "text": text,
            "model": llm_client.model_name(),
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "facts_hash": digest,
        }
        generated += 1

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=1))
    log.info("commentary: %d regenerated, %d total", generated, len(out))
    return out
