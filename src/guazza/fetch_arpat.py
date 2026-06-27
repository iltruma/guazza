"""Fetcher ARPAT — qualità aria NRT oraria (json_orari_nrt) + bollettino giornaliero PM10/PM2.5.

Output: righe wide per `observations` (granularity 'hourly' per NRT, 'daily' per bollettino).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

import httpx
from loguru import logger
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from guazza._logging import log_scrape
from guazza.fetch_common import (
    ITALY_TZ as _ITALY_TZ,
)
from guazza.fetch_common import (
    UA as _UA,
)
from guazza.fetch_common import (
    is_retryable_http as _is_retryable_http,
)

# ═════════════════════════════════════════════════════════════════════════════
# ARPAT OpenData NRT — Qualità aria oraria (real-time)
# ═════════════════════════════════════════════════════════════════════════════

_ARPAT_NRT_BASE = (
    "https://opendata.arpat.toscana.it/temi-ambientali/aria/qualita-aria"
    "/dati_orari_real_time/json_orari_nrt"
)

# Nome parametro ARPAT (UPPER) → (colonna schema, fattore unità)
# CO: ARPAT Toscana pubblica in mg/m³ (D.Lgs.155/2010) — schema usa mg/m³, fattore 1.0
_ARPAT_PARAM_MAP: dict[str, tuple[str, float]] = {
    "PM10":    ("pm10_ugm3",    1.0),
    "PM2.5":   ("pm25_ugm3",    1.0),
    "NO2":     ("no2_ugm3",     1.0),
    "O3":      ("o3_ugm3",      1.0),
    "CO":      ("co_mgm3",      1.0),
    "SO2":     ("so2_ugm3",     1.0),
    "BENZENE": ("benzene_ugm3", 1.0),
    "C6H6":    ("benzene_ugm3", 1.0),
}


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=10, max=60),
    retry=retry_if_exception(_is_retryable_http),
)
def _fetch_arpat_nrt_json(station_id: str) -> Any:
    """Fetch JSON NRT ARPAT per stazione (ultimi valori disponibili) con retry."""
    url = f"{_ARPAT_NRT_BASE}/{station_id}/last"
    with httpx.Client(timeout=10, headers={"User-Agent": _UA}) as client:
        r = client.get(url)
        r.raise_for_status()
    return r.json()


def _parse_arpat_nrt(
    payload: Any,
    station_id: str,
    location_id: str,
    weight: float,
) -> list[dict[str, Any]]:
    """Parsifica risposta JSON ARPAT NRT in record wide per upsert observations.

    Formato risposta: lista di dict orari con ORA ("00"-"23"),
    DATA_OSSERVAZIONE ("22-MAY-26") e parametri come valori numerici o null.
    """
    if not isinstance(payload, list):
        return []

    records: list[dict[str, Any]] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue

        ora_str = str(entry.get("ORA") or "").strip().zfill(2)
        data_str = str(entry.get("DATA_OSSERVAZIONE") or "").strip()
        if not ora_str or not data_str:
            continue
        try:
            naive_local = datetime.strptime(f"{ora_str} {data_str}", "%H %d-%b-%y")
            ts = naive_local.replace(tzinfo=_ITALY_TZ).astimezone(UTC).replace(tzinfo=None)
        except ValueError:
            logger.debug(
                f"ARPAT NRT [{station_id}] timestamp non parsificabile: "
                f"ORA={ora_str!r} DATA={data_str!r}"
            )
            continue

        rec: dict[str, Any] = {
            "source": "arpat",
            "station_id": station_id,
            "location_id": location_id,
            "ts": ts,
            "granularity": "hourly",
            "weight": weight,
            "qc_pass": True,
        }
        has_data = False
        for param_key, (col, factor) in _ARPAT_PARAM_MAP.items():
            raw = entry.get(param_key)
            if raw is None:
                continue
            try:
                val = float(str(raw).replace(",", "."))
                rec[col] = round(val * factor, 4)
                has_data = True
            except (ValueError, TypeError):
                pass

        if has_data:
            records.append(rec)

    return records


def fetch_arpat_nrt_station(
    station_id: str,
    location_id: str,
    weight: float,
) -> list[dict[str, Any]]:
    """Fetch dati qualità aria NRT per una stazione ARPAT (endpoint /last).

    Returns: lista record wide per upsert su observations.
             Lista vuota su fallimento — il prossimo cron riprova.
    """
    try:
        payload = _fetch_arpat_nrt_json(station_id)
        records = _parse_arpat_nrt(payload, station_id, location_id, weight)
        log_scrape(f"arpat_nrt:{station_id}", "ok", rows=len(records))
        return records
    except Exception as e:
        log_scrape(f"arpat_nrt:{station_id}", "fail", detail=str(e))
        return []


def fetch_arpat_all_locations(
    locations: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """Fetch ARPAT NRT per tutte le location con extras: [aria_qualita].

    Usa l'endpoint /last — restituisce gli ultimi valori disponibili per ogni stazione.
    Le richieste sono eseguite in parallelo (4 worker) per ridurre il tempo totale
    in caso di endpoint lento: 24 chiamate seriali con timeout = fino a 72 min,
    parallelo = ~3-4 min worst case.

    Args:
        locations: dict locations da locations.yaml["locations"].

    Returns:
        Dict {location_id: [record, ...]} — location con 0 record inclusa se configurata.
    """
    # Raccogli le coppie (station_id, location_id, weight) deduplicando per station_id.
    tasks: list[tuple[str, str, float]] = []
    seen_stations: set[str] = set()
    seen_locations: set[str] = set()
    for loc_id, loc in locations.items():
        if "aria_qualita" not in (loc.get("extras") or []):
            continue
        seen_locations.add(loc_id)
        for station in (loc.get("arpat_stations") or []):
            sid: str = station["id"]
            w: float = float(station.get("weight", 1.0))
            if sid in seen_stations:
                continue
            seen_stations.add(sid)
            tasks.append((sid, loc_id, w))

    if not tasks:
        return {}

    results: dict[str, list[dict[str, Any]]] = {loc_id: [] for loc_id in seen_locations}

    with ThreadPoolExecutor(max_workers=4) as ex:
        future_to_loc = {
            ex.submit(fetch_arpat_nrt_station, sid, loc_id, w): loc_id
            for sid, loc_id, w in tasks
        }
        for fut in future_to_loc:
            loc_id = future_to_loc[fut]
            try:
                records = fut.result()
                results[loc_id].extend(records)
            except Exception as e:
                # fetch_arpat_nrt_station già logga fail via log_scrape; qui
                # catturiamo solo l'eventuale eccezione dal future (non dovrebbe).
                logger.warning(f"ARPAT future {loc_id} unexpected error: {e}")

    return results


# ── ARPAT bollettino giornaliero (PM10 / PM2.5) ───────────────────────────────

_ARPAT_BOLLETTINO_URL = (
    "https://opendata.arpat.toscana.it/temi-ambientali/aria/qualita-aria"
    "/bollettini/bollettino_json/"
)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=10, max=60),
    retry=retry_if_exception(_is_retryable_http),
)
def _fetch_arpat_bollettino_json() -> Any:
    """Fetch ultimo bollettino ARPAT Toscana (un endpoint per tutta la regione)."""
    with httpx.Client(timeout=10, headers={"User-Agent": _UA}) as client:
        r = client.get(_ARPAT_BOLLETTINO_URL)
        r.raise_for_status()
    return r.json()


def _parse_arpat_bollettino(
    payload: Any,
    station_location_map: dict[str, tuple[str, float]],
) -> list[dict[str, Any]]:
    """Parsifica bollettino ARPAT: estrae solo PM10 e PM2.5 per le stazioni configurate.

    Args:
        payload: risposta JSON bollettino (lista di dict stazione).
        station_location_map: {station_id -> (location_id, weight)} per le stazioni
            configurate. Solo le stazioni presenti nella mappa vengono importate.

    Returns:
        Lista di record wide con granularity='daily', ts=mezzanotte del giorno bollettino.
    """
    if not isinstance(payload, list):
        return []

    records: list[dict[str, Any]] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue

        station_id: str = str(entry.get("NOME_STAZIONE") or "").strip()
        if station_id not in station_location_map:
            continue

        data_str = str(entry.get("DATA_OSSERVAZIONE") or "").strip()
        if not data_str:
            continue
        try:
            ts = datetime.strptime(data_str, "%d-%b-%y")
        except ValueError:
            logger.debug(f"ARPAT bollettino [{station_id}] DATA non parsificabile: {data_str!r}")
            continue

        location_id, weight = station_location_map[station_id]

        def _boll_val(raw: Any) -> float | None:
            if raw is None:
                return None
            s = str(raw).strip().replace(",", ".")
            if s in ("-", "n.d.", ""):
                return None
            try:
                return float(s)
            except ValueError:
                return None

        pm10 = _boll_val(entry.get("PM10"))
        pm25 = _boll_val(entry.get("PM2dot5"))
        if pm10 is None and pm25 is None:
            continue

        records.append({
            "source": "arpat",
            "station_id": station_id,
            "location_id": location_id,
            "ts": ts,
            "granularity": "daily",
            "weight": weight,
            "qc_pass": True,
            "pm10_ugm3": pm10,
            "pm25_ugm3": pm25,
        })

    return records


def fetch_arpat_bollettino_all_locations(
    locations: dict[str, Any],
) -> list[dict[str, Any]]:
    """Fetch bollettino ARPAT PM10/PM2.5 per tutte le location configurate.

    Returns:
        Lista record wide per upsert su observations (granularity='daily').
        Lista vuota su fallimento — il prossimo cron riprova.
    """
    station_location_map: dict[str, tuple[str, float]] = {}
    for loc_id, loc in locations.items():
        if "aria_qualita" not in (loc.get("extras") or []):
            continue
        for station in (loc.get("arpat_stations") or []):
            sid: str = station["id"]
            w: float = float(station.get("weight", 1.0))
            if sid not in station_location_map:
                station_location_map[sid] = (loc_id, w)

    if not station_location_map:
        return []

    try:
        payload = _fetch_arpat_bollettino_json()
        records = _parse_arpat_bollettino(payload, station_location_map)
        log_scrape("arpat_bollettino", "ok", rows=len(records))
        return records
    except Exception as e:
        log_scrape("arpat_bollettino", "fail", detail=str(e))
        return []

