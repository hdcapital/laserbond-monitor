"""Thin OpenAI Chat Completions client (PDF document extraction).

Model selection order:
  1. OPENAI_MODEL environment variable (in CI this is fed from the
     repository *variable* OPENAI_MODEL, so the model is switchable
     without touching code)
  2. config.yaml -> openai.model
"""
from __future__ import annotations

import base64
import json
import logging
import os

import requests

from .config import cfg

log = logging.getLogger("lbl_tracker.llm")

API_URL = "https://api.openai.com/v1/chat/completions"


def have_key() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY", "").strip())


def model_name() -> str:
    return (os.environ.get("OPENAI_MODEL", "").strip()
            or cfg("openai", "model", default="gpt-5-mini"))


def complete(prompt: str, max_tokens: int = 400) -> str:
    """Plain text completion (used for dashboard commentary)."""
    if not have_key():
        raise RuntimeError("OPENAI_API_KEY not set")
    resp = requests.post(API_URL, json={
        "model": model_name(),
        "max_completion_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }, timeout=120, headers={
        "Authorization": f"Bearer {os.environ['OPENAI_API_KEY'].strip()}",
        "content-type": "application/json",
    })
    if resp.status_code != 200:
        raise RuntimeError(f"openai API {resp.status_code}: {resp.text[:400]}")
    return (resp.json()["choices"][0]["message"]["content"] or "").strip()


def extract_json_from_text(text: str, prompt: str,
                           max_tokens: int | None = None) -> dict:
    """Send plain text + prompt, expect a single JSON object back."""
    if not have_key():
        raise RuntimeError("OPENAI_API_KEY not set")
    resp = requests.post(API_URL, json={
        "model": model_name(),
        "max_completion_tokens": max_tokens
        or cfg("openai", "max_output_tokens", default=2048),
        "response_format": {"type": "json_object"},
        "messages": [{"role": "user",
                      "content": prompt + "\nRespond with a single JSON object "
                                          "only.\n\nDOCUMENT:\n" + text}],
    }, timeout=180, headers={
        "Authorization": f"Bearer {os.environ['OPENAI_API_KEY'].strip()}",
        "content-type": "application/json",
    })
    if resp.status_code != 200:
        raise RuntimeError(f"openai API {resp.status_code}: {resp.text[:400]}")
    reply = resp.json()["choices"][0]["message"]["content"] or ""
    start, end = reply.find("{"), reply.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON object in model reply: {reply[:300]}")
    return json.loads(reply[start:end + 1])


def extract_json_from_pdf(pdf_bytes: bytes, prompt: str) -> dict:
    """Send a PDF + prompt, expect a single JSON object back."""
    if not have_key():
        raise RuntimeError("OPENAI_API_KEY not set")
    body = {
        "model": model_name(),
        "max_completion_tokens": cfg("openai", "max_output_tokens", default=2048),
        "response_format": {"type": "json_object"},
        "messages": [{
            "role": "user",
            "content": [
                {"type": "file",
                 "file": {"filename": "announcement.pdf",
                          "file_data": "data:application/pdf;base64,"
                                       + base64.standard_b64encode(pdf_bytes).decode()}},
                {"type": "text",
                 "text": prompt + "\nRespond with a single JSON object only."},
            ],
        }],
    }
    resp = requests.post(API_URL, json=body, timeout=300, headers={
        "Authorization": f"Bearer {os.environ['OPENAI_API_KEY'].strip()}",
        "content-type": "application/json",
    })
    if resp.status_code != 200:
        raise RuntimeError(f"openai API {resp.status_code}: {resp.text[:400]}")
    text = resp.json()["choices"][0]["message"]["content"] or ""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON object in model reply: {text[:300]}")
    return json.loads(text[start:end + 1])
