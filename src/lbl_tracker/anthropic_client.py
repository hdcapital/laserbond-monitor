"""Thin Anthropic Messages API client (PDF document extraction)."""
from __future__ import annotations

import base64
import json
import logging
import os

import requests

from .config import cfg

log = logging.getLogger("lbl_tracker.anthropic")

API_URL = "https://api.anthropic.com/v1/messages"


def have_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


def extract_json_from_pdf(pdf_bytes: bytes, prompt: str) -> dict:
    """Send a PDF + prompt, expect a single JSON object back."""
    if not have_key():
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    body = {
        "model": cfg("anthropic", "model", default="claude-sonnet-5"),
        "max_tokens": cfg("anthropic", "max_tokens", default=2048),
        "messages": [{
            "role": "user",
            "content": [
                {"type": "document",
                 "source": {"type": "base64", "media_type": "application/pdf",
                            "data": base64.standard_b64encode(pdf_bytes).decode()}},
                {"type": "text", "text": prompt},
            ],
        }],
    }
    resp = requests.post(API_URL, json=body, timeout=300, headers={
        "x-api-key": os.environ["ANTHROPIC_API_KEY"].strip(),
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    })
    if resp.status_code != 200:
        raise RuntimeError(f"anthropic API {resp.status_code}: {resp.text[:400]}")
    text = "".join(block.get("text", "") for block in resp.json().get("content", []))
    # tolerate a fenced or prefixed JSON answer
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON object in model reply: {text[:300]}")
    return json.loads(text[start:end + 1])
