"""Monthly email brief via SMTP.

Requires SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM,
SMTP_TO. Fails loudly when secrets are missing so a silently-dead brief
can't go unnoticed. The HTML uses inline styles only (email clients strip
stylesheets); colors mirror the dashboard palette.
"""
from __future__ import annotations

import html
import json
import logging
import os
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from .config import REPO_ROOT, cfg

log = logging.getLogger("lbl_tracker.email")

REQUIRED = ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM", "SMTP_TO"]

INK = "#0b0b0b"
INK2 = "#52514e"
MUTED = "#898781"
BLUE = "#2a78d6"
RED = "#e34948"
SERIOUS = "#b95328"
HAIRLINE = "#e1e0d9"
SURFACE = "#fcfcfb"


def _require_env() -> dict:
    missing = [k for k in REQUIRED if not os.environ.get(k, "").strip()]
    if missing:
        raise RuntimeError(f"email brief: missing SMTP secrets: {missing}")
    return {k: os.environ[k].strip() for k in REQUIRED}


def _esc(value) -> str:
    return html.escape(str(value if value is not None else "—"))


def _num(value):
    """None for missing/NaN, float otherwise (parquet round-trips NaN)."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return None if value != value else value


def _aud(value) -> str:
    value = _num(value)
    if value is None:
        return "—"
    if abs(value) >= 1e6:
        return f"A${value / 1e6:,.1f}m"
    if abs(value) >= 1e3:
        return f"A${value / 1e3:,.0f}k"
    return f"A${value:,.0f}"


def _chip(value) -> str:
    """Sign-colored value chip; NO DATA in muted gray."""
    if value is None:
        return (f'<span style="display:inline-block;padding:2px 10px;border-radius:10px;'
                f'background:#f0efec;color:{MUTED};font-weight:600;font-size:13px;">'
                f'NO DATA</span>')
    color = BLUE if value >= 0 else RED
    sign = "+" if value > 0 else ""
    return (f'<span style="display:inline-block;padding:2px 10px;border-radius:10px;'
            f'background:{color};color:#ffffff;font-weight:700;font-size:14px;">'
            f'{sign}{value:.1f}</span>')


def _delta(history: list) -> str:
    """Month-on-month change from the pulse history, if both months exist."""
    vals = [h["value"] for h in history if h.get("value") is not None]
    if len(vals) < 2:
        return ""
    change = vals[-1] - vals[-2]
    arrow = "▲" if change > 0 else ("▼" if change < 0 else "◆")
    color = BLUE if change > 0 else (RED if change < 0 else MUTED)
    return (f'<span style="color:{color};font-size:12px;font-weight:600;">'
            f'{arrow} {change:+.1f} m/m</span>')


def _section(title: str, body: str, subtitle: str = "") -> str:
    sub = (f'<span style="color:{MUTED};font-weight:400;font-size:12px;"> — {subtitle}'
           f'</span>' if subtitle else "")
    return (f'<tr><td style="padding:22px 28px 0 28px;">'
            f'<div style="font-size:15px;font-weight:700;color:{INK};'
            f'padding-bottom:8px;border-bottom:1px solid {HAIRLINE};">{title}{sub}</div>'
            f'</td></tr><tr><td style="padding:10px 28px 4px 28px;">{body}</td></tr>')


def render() -> tuple[str, str, str]:
    """Return (subject, text_body, html_body) from the built dashboard data."""
    path = REPO_ROOT / "docs" / "data" / "dashboard.json"
    if not path.exists():
        raise RuntimeError("email brief: docs/data/dashboard.json missing - run "
                           "`lbl-tracker pulses && lbl-tracker dashboard` first")
    data = json.loads(path.read_text())
    month = datetime.now(timezone.utc).strftime("%B %Y")
    subject = f"LBL Tracker brief — {month}"
    dash_url = cfg("dashboard", "url", default="")

    pulses = data.get("pulses", {}).get("pulses", {})
    tech = data.get("pulses", {}).get("technology", {})
    freshness = data.get("freshness", [])
    stale = [f for f in freshness if f["status"] == "STALE"]
    nodata = [f for f in freshness if f["status"] == "NO DATA"]
    discontinued = [f for f in freshness if f["status"] == "DISCONTINUED"]

    # ---- text version -----------------------------------------------------
    lines = [f"LBL Tracker monthly brief ({month})", ""]
    for name, p in pulses.items():
        val = p.get("latest_value")
        lines.append(f"  {p.get('title', name):<18} "
                     f"{'NO DATA' if val is None else f'{val:+.1f}':>8}"
                     f"   as of {p.get('latest_month') or '—'}")
    lines.append("")
    if tech.get("available"):
        counts = tech.get("stage_counts", {})
        lines.append("Technology pipeline (facts, never scored):")
        lines.append("  " + "  ".join(f"{k}:{v}" for k, v in counts.items()))
        lines.append(f"  contracted (where stated): {_aud(tech.get('contracted_value_aud_where_stated'))}"
                     f" | unrecognised: {_aud(tech.get('contracted_unrecognised_aud_where_stated'))}")
    else:
        lines.append("Technology pipeline: NO DATA")
    if stale:
        lines.append("")
        lines.append("Stale series (check source):")
        for f in stale:
            lines.append(f"  - {f['label']} (last: {f['last_date']})")
    if nodata:
        lines.append("")
        lines.append("Awaiting first data: " + ", ".join(f["label"] for f in nodata))
    lines += ["", f"Dashboard: {dash_url}", "",
              "Nothing in this brief is estimated; missing data is shown as NO DATA."]

    # ---- html version -----------------------------------------------------
    pulse_rows = ""
    for name, p in pulses.items():
        pulse_rows += (
            f'<tr>'
            f'<td style="padding:9px 0;font-size:14px;color:{INK};'
            f'border-bottom:1px solid {HAIRLINE};">{_esc(p.get("title", name))}'
            f'<div style="font-size:11px;color:{MUTED};">as of '
            f'{_esc(p.get("latest_month"))}</div></td>'
            f'<td style="padding:9px 0 9px 12px;text-align:right;white-space:nowrap;'
            f'border-bottom:1px solid {HAIRLINE};">{_chip(p.get("latest_value"))}'
            f'<div style="padding-top:3px;">{_delta(p.get("history", []))}</div></td>'
            f'</tr>')
    pulses_html = (f'<table role="presentation" width="100%" cellpadding="0" '
                   f'cellspacing="0">{pulse_rows}</table>'
                   f'<div style="font-size:11px;color:{MUTED};padding-top:8px;">'
                   f'−100…+100 z-score composites vs the trailing 5 years; weights '
                   f'renormalise over available components.</div>')

    if tech.get("available"):
        counts = tech.get("stage_counts", {})
        deltas = tech.get("stage_deltas_6m", {})
        stage_cells = ""
        for stage in tech.get("stages", []):
            d = deltas.get(stage, 0)
            delta_txt = (f'<div style="font-size:10px;color:{MUTED};">{d:+d} / 6m</div>'
                         if d else '<div style="font-size:10px;color:{0};">&nbsp;</div>'.format(MUTED))
            stage_cells += (
                f'<td align="center" style="padding:8px 4px;border:1px solid {HAIRLINE};'
                f'border-radius:6px;background:#f9f9f7;">'
                f'<div style="font-size:18px;font-weight:700;color:{INK};">'
                f'{counts.get(stage, 0)}</div>'
                f'<div style="font-size:10px;color:{INK2};">'
                f'{_esc(stage.replace("_", " "))}</div>{delta_txt}</td>'
                f'<td style="width:6px;"></td>')
        events_rows = ""
        for e in (tech.get("events") or [])[:5]:
            val = (f' · {_aud(e.get("value_aud"))}'
                   if _num(e.get("value_aud")) is not None else "")
            cp = e.get("counterparty")
            counterparty = (f'{_esc(cp)} — '
                            if cp and str(cp).lower() != "nan" else "")
            events_rows += (
                f'<div style="padding:6px 0;border-bottom:1px solid {HAIRLINE};'
                f'font-size:12px;color:{INK2};">'
                f'<span style="color:{MUTED};">{_esc(e.get("date"))}</span> · '
                f'<b style="color:{INK};">{_esc(e.get("stage"))}</b>{val}<br>'
                f'{counterparty}{_esc(e.get("description"))}</div>')
        tech_html = (
            f'<table role="presentation" cellpadding="0" cellspacing="0" width="100%">'
            f'<tr>{stage_cells}</tr></table>'
            f'<div style="font-size:13px;color:{INK};padding:10px 0 4px 0;">'
            f'Contracted (where stated): <b>{_aud(tech.get("contracted_value_aud_where_stated"))}</b>'
            f' &nbsp;·&nbsp; recognised: <b>{_aud(tech.get("recognised_aud_where_stated")) if _num(tech.get("recognised_aud_where_stated")) else "none stated"}</b>'
            f' &nbsp;·&nbsp; contracted-unrecognised: '
            f'<b>{_aud(tech.get("contracted_unrecognised_aud_where_stated"))}</b></div>'
            f'{events_rows}')
    else:
        tech_html = (f'<div style="font-size:13px;color:{MUTED};">NO DATA — '
                     f'{_esc(tech.get("note", ""))}</div>')

    health_bits = []
    if stale:
        rows = "".join(
            f'<div style="font-size:12px;color:{INK2};padding:3px 0;">'
            f'<span style="color:{SERIOUS};font-weight:700;">⚠ STALE</span> '
            f'{_esc(f["label"])} <span style="color:{MUTED};">(last '
            f'{_esc(f["last_date"])})</span></div>' for f in stale)
        health_bits.append(rows)
    if nodata:
        health_bits.append(
            f'<div style="font-size:12px;color:{MUTED};padding:3px 0;">∅ Awaiting '
            f'first data: {_esc(", ".join(f["label"] for f in nodata))}</div>')
    if discontinued:
        health_bits.append(
            f'<div style="font-size:11px;color:{MUTED};padding:3px 0;">◦ Discontinued '
            f'by publisher (kept for backtests): '
            f'{_esc(", ".join(f["label"] for f in discontinued))}</div>')
    health_html = "".join(health_bits) or (
        f'<div style="font-size:13px;color:{INK2};">✓ All live series current.</div>')

    button = (f'<a href="{_esc(dash_url)}" style="display:inline-block;padding:9px 18px;'
              f'background:{BLUE};color:#ffffff;text-decoration:none;border-radius:7px;'
              f'font-size:13px;font-weight:600;">Open the dashboard</a>'
              if dash_url else "")

    html_body = f"""<!doctype html><html><body style="margin:0;padding:0;background:#f0efec;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f0efec;padding:18px 0;"><tr><td align="center">
<table role="presentation" width="600" cellpadding="0" cellspacing="0"
  style="background:{SURFACE};border:1px solid {HAIRLINE};border-radius:12px;
  font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
<tr><td style="padding:24px 28px 4px 28px;">
  <div style="font-size:19px;font-weight:800;color:{INK};">LBL Tracker</div>
  <div style="font-size:12px;color:{MUTED};">Monthly brief · {_esc(month)} ·
  external-data nowcast for LaserBond (ASX:LBL)</div>
</td></tr>
{_section("Pulses", pulses_html)}
{_section("Technology pipeline", tech_html, "facts from classified announcements — never scored")}
{_section("Data health", health_html)}
<tr><td style="padding:18px 28px 6px 28px;">{button}</td></tr>
<tr><td style="padding:10px 28px 22px 28px;">
  <div style="font-size:10px;color:{MUTED};line-height:1.5;">
  Nothing in this brief is estimated, interpolated or backfilled; missing data is
  shown as NO DATA. Every datapoint carries its source URL and retrieval time —
  see SOURCES.md in the repository.</div>
</td></tr>
</table></td></tr></table></body></html>"""

    return subject, "\n".join(lines), html_body


def send() -> None:
    env = _require_env()
    subject, text, html_body = render()
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = env["SMTP_FROM"]
    recipients = [r.strip() for r in env["SMTP_TO"].split(",") if r.strip()]
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html_body, "html"))
    port = int(env["SMTP_PORT"])
    with smtplib.SMTP(env["SMTP_HOST"], port, timeout=60) as server:
        server.ehlo()
        if port != 465:
            server.starttls()
            server.ehlo()
        server.login(env["SMTP_USER"], env["SMTP_PASSWORD"])
        server.sendmail(env["SMTP_FROM"], recipients, msg.as_string())
    log.info("email brief sent to %s", recipients)
