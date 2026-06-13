"""Costanti e helper HTTP condivisi dai fetcher (fetch_sir, fetch_openmeteo, fetch_netatmo, fetch_arpat)."""

from __future__ import annotations

import zoneinfo
from datetime import timedelta, timezone

import httpx

# User-Agent comune a tutti gli scraper
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

# Timezone helpers — tutte le osservazioni nel DB sono UTC naive.
# SIR pubblica sempre CET (UTC+1 fisso, non cambia con l'ora legale)
CET = timezone(timedelta(hours=1))
# ARPAT pubblica ora locale italiana (CET in inverno, CEST in estate)
ITALY_TZ = zoneinfo.ZoneInfo("Europe/Rome")


def is_retryable_http(exc: BaseException) -> bool:
    """Ritenta su 429, 5xx e errori di trasporto transitori (timeout, connection reset).
    JSONDecodeError e altri errori applicativi non si ritentano — sono permanenti."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    if isinstance(exc, httpx.TransportError):
        return True
    return False
