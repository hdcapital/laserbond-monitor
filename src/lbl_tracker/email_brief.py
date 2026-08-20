"""Monthly email brief via SMTP.

Requires SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM,
SMTP_TO. Fails loudly when secrets are missing so a silently-dead brief
can't go unnoticed.
"""
from __future__ import annotations

import json
import logging
import os
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from .config import REPO_ROOT

log = logging.getLogger("lbl_tracker.email")

REQUIRED = ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM", "SMTP_TO"]


def _require_env() -> dict:
    missing = [k for k in REQUIRED if not os.environ.get(k, "").strip()]
    if missing:
        raise RuntimeError(f"email brief: missing SMTP secrets: {missing}")
    return {k: os.environ[k].strip() for k in REQUIRED}


def render() -> tuple[str, str, str]:
    """Return (subject, text_body, html_body) from the built dashboard data."""
    path = REPO_ROOT / "docs" / "data" / "dashboard.json"
    if not path.exists():
        raise RuntimeError("email brief: docs/data/dashboard.json missing - run "
                           "`lbl-tracker pulses && lbl-tracker dashboard` first")
    data = json.loads(path.read_text())
    month = datetime.now(timezone.utc).strftime("%B %Y")
    subject = f"LBL Tracker brief - {month}"

    lines = [f"LBL Tracker monthly brief ({month})", ""]
    html = [f"<h2>LBL Tracker monthly brief ({month})</h2>",
            "<table border='0' cellpadding='4' style='font-family:sans-serif'>"]
    pulses = data.get("pulses", {}).get("pulses", {})
    for name, p in pulses.items():
        val = p.get("latest_value")
        val_s = "NO DATA" if val is None else f"{val:+.1f}"
        lines.append(f"{p.get('title', name)}: {val_s} (as of {p.get('latest_month')})")
        html.append(f"<tr><td><b>{p.get('title', name)}</b></td>"
                    f"<td>{val_s}</td><td>{p.get('latest_month') or ''}</td></tr>")
    html.append("</table>")

    tech = data.get("pulses", {}).get("technology", {})
    lines.append("")
    if tech.get("available"):
        lines.append("Technology pipeline (facts):")
        lines.append(f"  stage counts: {tech.get('stage_counts')}")
        lines.append(f"  6m deltas: {tech.get('stage_deltas_6m')}")
        lines.append("  contracted-unrecognised (where stated): "
                     f"A${tech.get('contracted_unrecognised_aud_where_stated'):,.0f}")
        html.append(f"<h3>Technology pipeline</h3><p>stage counts: {tech.get('stage_counts')}"
                    f"<br>6m deltas: {tech.get('stage_deltas_6m')}"
                    f"<br>contracted-unrecognised (where stated): "
                    f"A${tech.get('contracted_unrecognised_aud_where_stated'):,.0f}</p>")
    else:
        lines.append("Technology pipeline: NO DATA")
        html.append("<p>Technology pipeline: NO DATA</p>")

    stale = [f for f in data.get("freshness", []) if f["status"] != "OK"]
    lines.append("")
    lines.append(f"Series not OK ({len(stale)}):")
    html.append(f"<h3>Series not OK ({len(stale)})</h3><ul>")
    for f in stale:
        lines.append(f"  [{f['status']}] {f['label']} (last: {f['last_date'] or 'never'})")
        html.append(f"<li>[{f['status']}] {f['label']} (last: {f['last_date'] or 'never'})</li>")
    html.append("</ul><p>Full dashboard: GitHub Pages /docs.</p>")
    lines.append("")
    lines.append("Nothing in this brief is estimated; missing data is shown as NO DATA.")
    return subject, "\n".join(lines), "".join(html)


def send() -> None:
    env = _require_env()
    subject, text, html = render()
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = env["SMTP_FROM"]
    recipients = [r.strip() for r in env["SMTP_TO"].split(",") if r.strip()]
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))
    port = int(env["SMTP_PORT"])
    with smtplib.SMTP(env["SMTP_HOST"], port, timeout=60) as server:
        server.ehlo()
        if port != 465:
            server.starttls()
            server.ehlo()
        server.login(env["SMTP_USER"], env["SMTP_PASSWORD"])
        server.sendmail(env["SMTP_FROM"], recipients, msg.as_string())
    log.info("email brief sent to %s", recipients)
