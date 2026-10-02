from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
from loguru import logger

from app.config import settings

# Analytics posts run off the request path so a slow Supabase never delays a response.
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="supabase-events")


def _supabase_enabled() -> bool:
    # When the app's own database is Supabase, the usage middleware already
    # inserts the row there; forwarding it over REST would duplicate it.
    if settings.uses_supabase_db:
        return False
    return bool(settings.supabase_url and settings.supabase_key)


def _table_url() -> str:
    base = settings.supabase_url.rstrip("/")
    return f"{base}/rest/v1/{settings.supabase_analytics_table}"


def _post_event(url: str, headers: dict[str, str], payload: dict[str, Any]) -> None:
    try:
        response = httpx.post(url, headers=headers, json=payload, timeout=10.0)
        response.raise_for_status()
    except Exception as exc:
        # Best-effort analytics; never fail the app if Supabase is unreachable.
        logger.warning("Supabase analytics event not recorded: {}", exc)


def record_api_event(payload: dict[str, Any]) -> None:
    if not _supabase_enabled():
        return

    headers = {
        "apikey": settings.supabase_key,
        "Authorization": f"Bearer {settings.supabase_key}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    _executor.submit(_post_event, _table_url(), headers, dict(payload))
