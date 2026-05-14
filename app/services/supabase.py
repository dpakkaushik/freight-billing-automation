from __future__ import annotations

import json
from typing import Any

import httpx

from app.config import settings


def _supabase_enabled() -> bool:
    return bool(settings.supabase_url and settings.supabase_key)


def _table_url() -> str:
    base = settings.supabase_url.rstrip("/")
    return f"{base}/rest/v1/{settings.supabase_analytics_table}"


def record_api_event(payload: dict[str, Any]) -> None:
    if not _supabase_enabled():
        return

    headers = {
        "apikey": settings.supabase_key,
        "Authorization": f"Bearer {settings.supabase_key}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    try:
        response = httpx.post(_table_url(), headers=headers, json=payload, timeout=10.0)
        response.raise_for_status()
    except Exception:
        # This is best-effort analytics; do not fail the app if Supabase is unreachable.
        return
