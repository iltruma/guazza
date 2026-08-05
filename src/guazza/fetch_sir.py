"""Fetcher SIR Toscana — storico CSV (download.php), realtime JSON e bulk (actions.php).

Output: righe wide (una per stazione per timestamp) compatibili con INSERT in `observations`.
"""

from __future__ import annotations

import csv
import io
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from typing import Any

import httpx
from loguru import logger
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)
from tqdm import tqdm

from guazza._logging import log_scrape
from guazza.fetch_common import CET as _CET
from guazza.fetch_common import UA as _UA
from guazza.fetch_common import is_retryable_http as _is_retryable_http

# ── Costanti SIR CSV ──────────────────────────────────────────────────────────

_SIR_BASE_URL = "https://www.sir.toscana.it/archivio/download.php"
_SIR_HEADERS = {"X-Requested-With": "XMLHttpRequest", "User-Agent": _UA}

_WIND_DIR_DEG: dict[str, float] = {
    "N": 0.0, "NNE": 22.5, "NE": 45.0, "ENE": 67.5,
    "E": 90.0, "ESE": 112.5, "SE": 135.0, "SSE": 157.5,
    "S": 180.0, "SSO": 202.5, "SO": 225.0, "OSO": 247.5,
    "O": 270.0, "ONO": 292.5, "NO": 315.0, "NNO": 337.5,
}

_FLAG_MAP: dict[str, str] = {
    "V": "ok", "N": "ok", "P": "ok",
    "R": "reconstructed", "I": "uncertain", "@": "missing",
}

# Schema SIR CSV: nomi colonna interni e se esiste colonna flag
_SENSOR_SCHEMA: dict[str, dict[str, Any]] = {
    "termo_csv": {
        "variables": [("tmax_c", float), ("tmin_c", float)],
        "flag_col": False,
    },
    "pluvio0_24": {
        "variables": [("precip_mm", float)],
        "flag_col": True,
    },
    "igro0_24": {
        "variables": [("hum_med_pct", float), ("hum_min_pct", float), ("hum_max_pct", float)],
        "flag_col": False,
    },
    "anemo0_24": {
        "variables": [("wind_speed_ms", float), ("wind_dir_deg", str), ("wind_gust_ms", float)],
        "flag_col": False,
    },
    "idro_l": {
        "variables": [("level_m", float)],
        "flag_col": True,
    },
}

# ── Costanti SIR realtime ─────────────────────────────────────────────────────

_SIR_REALTIME_BASE = "https://www.sir.toscana.it/monitoraggio"
_SIR_RT_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.sir.toscana.it/",
    "User-Agent": _UA,
}
# Formati data noti nella risposta SIR realtime
_SIR_RT_DATE_FORMATS = ["%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S"]


# ═════════════════════════════════════════════════════════════════════════════
# SIR — Storico CSV
# ═════════════════════════════════════════════════════════════════════════════


def _parse_value(raw: str, internal_name: str) -> float | None:
    s = raw.strip()
    if not s:
        return None
    if internal_name == "wind_dir_deg":
        deg = _WIND_DIR_DEG.get(s.upper())
        if deg is None:
            logger.debug(f"Direzione vento sconosciuta: {s!r}")
        return deg
    try:
        return float(s.replace(",", "."))
    except ValueError:
        logger.debug(f"Valore non parsabile: {s!r} per {internal_name!r}")
        return None


def _log_sir_retry(retry_state: RetryCallState) -> None:
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    if isinstance(exc, httpx.HTTPStatusError):
        logger.warning(
            f"SIR CSV HTTP {exc.response.status_code} "
            f"(attempt {retry_state.attempt_number}): {exc.request.url}"
        )
    else:
        logger.warning(f"SIR CSV retry (attempt {retry_state.attempt_number}): {exc}")


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=15),
    retry=retry_if_exception(_is_retryable_http),
    before_sleep=_log_sir_retry,
    reraise=True,
)
def _fetch_sir_csv(station_id: str, sensor_type: str) -> str:
    logger.debug(f"SIR CSV fetch: {station_id} {sensor_type}")
    with httpx.Client(timeout=60) as client:
        r = client.get(
            _SIR_BASE_URL,
            params={"IDST": sensor_type, "IDS": station_id},
            headers=_SIR_HEADERS,
        )
        r.raise_for_status()
    return r.text


def fetch_sir_historical(
    station_id: str,
    sensor_type: str,
    location_id: str = "",
) -> list[dict[str, Any]]:
    """Scarica e parsa tutto lo storico CSV per una stazione SIR.

    Returns:
        Lista di dict wide (una riga per giorno) con chiavi:
        source, station_id, location_id, ts, + colonne del sensore.
        Compatibile con UPSERT su `observations` (PK source+station_id+ts).
    """
    schema = _SENSOR_SCHEMA.get(sensor_type)
    if schema is None:
        raise ValueError(
            f"sensor_type {sensor_type!r} non supportato. Valori: {list(_SENSOR_SCHEMA)}"
        )

    text = _fetch_sir_csv(station_id, sensor_type)
    variables: list[tuple[str, str]] = schema["variables"]

    lines = text.splitlines()
    data_start = 0
    for i, line in enumerate(lines):
        if "gg/mm/aaaa" in line:
            data_start = i + 1
            break
    if data_start == 0:
        logger.warning(f"Header 'gg/mm/aaaa' non trovato: {station_id} {sensor_type}")
        return []

    records: list[dict[str, Any]] = []
    reader = csv.reader(io.StringIO("\n".join(lines[data_start:])), delimiter=";")

    for row in reader:
        if not row or not row[0].strip():
            continue
        date_str = row[0].strip()
        try:
            ts = datetime.strptime(date_str, "%d/%m/%Y")
        except ValueError:
            logger.debug(f"Data non parsabile: {date_str!r}")
            continue

        record: dict[str, Any] = {
            "source": "sir_toscana",
            "station_id": station_id,
            "location_id": location_id,
            "ts": ts,
            "granularity": "daily",
        }

        for i, (var_name, _type) in enumerate(variables):
            col_idx = i + 1
            raw = row[col_idx].strip() if col_idx < len(row) else ""
            value = _parse_value(raw, var_name)

            record[var_name] = value

        records.append(record)

    # precip_interval_h: solo per pluvio0_24 (24h) e idro_l non ha precip
    if sensor_type == "pluvio0_24":
        for r in records:
            r["precip_interval_h"] = 24

    return records


# ═════════════════════════════════════════════════════════════════════════════
# SIR — Realtime JSON
# ═════════════════════════════════════════════════════════════════════════════

def _parse_sir_realtime_ts(data: dict[str, Any]) -> datetime:
    """Estrae il timestamp dalla risposta JSON SIR realtime come datetime UTC naive.

    SIR pubblica sempre CET (UTC+1 fisso, non cambia con l'ora legale).
    Convertiamo a UTC naive prima di restituire, in linea con la convenzione
    del DB (tutte le osservazioni realtime in UTC naive).

    Tenta di parsare il campo "date" dal primo sensore disponibile (termo, igro, anemo).
    Fallback a now() UTC naive se assente o non parsabile.
    """
    for sensor_key in ("termo", "igro", "anemo"):
        sensor = data.get(sensor_key)
        if not sensor:
            continue
        date_str = sensor.get("date", "").strip()
        if not date_str:
            continue
        for fmt in _SIR_RT_DATE_FORMATS:
            try:
                naive_cet = datetime.strptime(date_str, fmt)
                return naive_cet.replace(tzinfo=_CET).astimezone(UTC).replace(tzinfo=None)
            except ValueError:
                continue
        logger.debug(f"SIR realtime: date non parsabile: {date_str!r}")
        break
    return datetime.now(UTC).replace(tzinfo=None)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def fetch_sir_realtime(station_id: str) -> dict[str, Any]:
    """Recupera letture real-time per una stazione SIR.

    Endpoint: /monitoraggio/actions.php?action=station&id=<station_id>

    Struttura JSON risposta:
      termo:  {"value": "13.0", "date": "..."}
      igro:   {"value": "90",   "date": "..."}
      anemo:  {"speed": "0.2",  "dir": "287", "date": "..."}   # dir già in gradi
      pluvio: {"CUM01": "3.4",  "CUM24": "5.4", ...}           # cumulativi multipli

    Returns:
        Dict wide con source, station_id, ts, e colonne meteo popolate.
    """
    url = f"{_SIR_REALTIME_BASE}/actions.php"
    params = {"action": "station", "id": station_id}
    logger.debug(f"SIR realtime: {station_id}")
    with httpx.Client(timeout=10) as client:
        r = client.get(url, params=params, headers=_SIR_RT_HEADERS)
        r.raise_for_status()
    data = r.json()
    time.sleep(1.0)

    # ts: proviamo a parsare il timestamp dalla risposta (primo sensore con campo "date").
    # Formato SIR atteso: "DD/MM/YYYY HH:MM" (locale Italy, ma SIR pubblica UTC+1 senza TZ).
    # Per sicurezza trattiamo come naive e aggiungiamo UTC (approssimazione accettabile per
    # osservazioni realtime dove il lag è già 10-15 min).
    ts = _parse_sir_realtime_ts(data)
    record: dict[str, Any] = {
        "source": "sir_toscana",
        "station_id": station_id,
        "ts": ts,
        "granularity": "realtime",
    }

    # termo
    if termo := data.get("termo"):
        if v := termo.get("value"):
            try:
                record["temp_c"] = float(v)
            except ValueError:
                pass

    # igro
    if igro := data.get("igro"):
        if v := igro.get("value"):
            try:
                record["humidity_pct"] = float(v)
            except ValueError:
                pass

    # anemo — dir è già in gradi decimali (no lookup _WIND_DIR_DEG)
    if anemo := data.get("anemo"):
        if v := anemo.get("speed"):
            try:
                record["wind_speed_ms"] = float(v)
            except ValueError:
                pass
        if d := anemo.get("dir"):
            try:
                record["wind_dir_deg"] = float(d)
            except ValueError:
                pass
        # vel_max non esposta dal nuovo endpoint — wind_gust_ms non popolata

    # pluvio — CUM01 = ultima ora (precip_mm), CUM24 = cumulativo dalla mezzanotte (precip_cumday_mm)
    if pluvio := data.get("pluvio"):
        cum01 = pluvio.get("CUM01")
        if cum01 is not None and cum01 != "-":
            try:
                record["precip_mm"] = float(cum01)
                record["precip_interval_h"] = 1
            except ValueError:
                pass
        cum24 = pluvio.get("CUM24")
        if cum24 is not None and cum24 != "-":
            try:
                record["precip_cumday_mm"] = float(cum24)
            except ValueError:
                pass

    # idro — livello idrometrico in metri (stazioni tipo meteo+idro o idro)
    if idro := data.get("idro"):
        if v := idro.get("value"):
            try:
                record["level_m"] = float(v)
            except ValueError:
                pass

    return record


def fetch_sir_stations_realtime(
    station_ids: list[str],
    max_workers: int = 5,
) -> dict[str, dict[str, Any]]:
    """Recupera real-time per una lista di stazioni in parallelo.

    Returns:
        Dict {station_id: record_wide} — le stazioni con errore vengono omesse.
    """
    results: dict[str, dict[str, Any]] = {}
    n_fail = 0
    _tty = sys.stderr.isatty()

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_sid = {
            executor.submit(fetch_sir_realtime, sid): sid
            for sid in station_ids
        }
        with tqdm(
            total=len(station_ids),
            desc="SIR realtime",
            unit="staz",
            disable=not _tty,
            dynamic_ncols=True,
        ) as bar:
            for future in as_completed(future_to_sid):
                sid = future_to_sid[future]
                try:
                    results[sid] = future.result()
                except Exception as e:
                    n_fail += 1
                    tqdm.write(f"SIR realtime fallito per {sid}: {e}", file=sys.stderr)
                bar.update(1)

    status = "ok" if results else "fail"
    detail = f"{n_fail} stazioni fallite" if n_fail else ""
    log_scrape("sir_realtime", status, rows=len(results), detail=detail)
    return results


# ═════════════════════════════════════════════════════════════════════════════
# SIR — Bulk realtime (TERMO24, IGRO24, ANEMO24, PLUVIO)
# ═════════════════════════════════════════════════════════════════════════════

def _parse_sir_bulk_meta_ts(meta_str: str) -> datetime | None:
    """Estrae datetime UTC naive dalla stringa meta SIR bulk.

    Formato atteso: ' del DD/MM/YYYY HH.MM (ora solare)'
    SIR bulk pubblica sempre CET (UTC+1 fisso). Convertiamo a UTC naive.
    Restituisce None se il formato non è riconoscibile.
    """
    m = re.search(r"(\d{2}/\d{2}/\d{4})\s+(\d{2}\.\d{2})", meta_str)
    if not m:
        return None
    try:
        naive_cet = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%d/%m/%Y %H.%M")
        return naive_cet.replace(tzinfo=_CET).astimezone(UTC).replace(tzinfo=None)
    except ValueError:
        return None


def _parse_bulk_float(raw: str | None) -> float | None:
    """Converte Valore da risposta bulk SIR in float.

    Valori non numerici noti: '&nbsp;+&nbsp;' (fuori scala alto),
    '&nbsp;-&nbsp;' (fuori scala basso), '' (assente).
    Un singolo '+' o '-' senza cifre non è un numero valido → None.
    """
    if raw is None:
        return None
    cleaned = raw.replace("&nbsp;", "").strip()
    if cleaned in ("", "+", "-", "N/A", "--", "ND"):
        return None
    try:
        return float(cleaned.replace(",", "."))
    except ValueError:
        return None


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=15),
    retry=retry_if_exception(_is_retryable_http),
    before_sleep=_log_sir_retry,
)
def _fetch_sir_bulk_json(action: str) -> dict[str, Any]:
    """Fetch JSON da un endpoint bulk SIR (TERMO24, IGRO24, ANEMO24, PLUVIO)."""
    logger.debug(f"SIR bulk fetch: {action}")
    with httpx.Client(timeout=10) as client:
        r = client.get(
            f"{_SIR_REALTIME_BASE}/actions.php",
            params={"action": action},
            headers=_SIR_RT_HEADERS,
        )
        r.raise_for_status()
    return r.json()  # type: ignore[no-any-return]


def _bulk_extract_anemo(entry: dict[str, Any]) -> dict[str, float | None]:
    """ANEMO24 porta due colonne: velocità (Valore) e direzione (Direzione)."""
    dir_raw = entry.get("Direzione")
    return {
        "wind_speed_ms": _parse_bulk_float(entry.get("Valore")),
        "wind_dir_deg": _parse_bulk_float(str(dir_raw) if dir_raw is not None else None),
    }


# Endpoint bulk SIR → extractor che mappa un entry JSON nelle colonne wide.
# PLUVIO: precip mm/15min, precip_interval_h resta NULL (TINYINT non supporta 0.25).
_SIR_BULK_ENDPOINTS: list[tuple[str, Any]] = [
    ("TERMO24", lambda e: {"temp_c": _parse_bulk_float(e.get("Valore"))}),
    ("IGRO24", lambda e: {"humidity_pct": _parse_bulk_float(e.get("Valore"))}),
    ("ANEMO24", _bulk_extract_anemo),
    ("PLUVIO", lambda e: {"precip_mm": _parse_bulk_float(e.get("Valore"))}),
]


def fetch_sir_bulk_realtime(station_ids: set[str]) -> dict[str, dict[str, Any]]:
    """Fetch realtime SIR via 4 endpoint bulk (TERMO24, IGRO24, ANEMO24, PLUVIO).

    4 HTTP call invece di N per-stazione. Ogni endpoint restituisce tutte le
    stazioni; filtriamo solo quelle in station_ids e mergiamo in record wide.

    precip_interval_h è NULL per i valori PLUVIO (intervallo 15min non
    rappresentabile come TINYINT nello schema).

    Returns:
        Dict {station_id: record_wide} per le stazioni trovate in almeno
        un endpoint. Stazioni offline in un endpoint avranno il campo a None.
    """
    combined: dict[str, dict[str, Any]] = {}

    def _ensure(sid: str, ts: datetime | None) -> dict[str, Any]:
        if sid not in combined:
            combined[sid] = {"ts": ts or datetime.now(UTC).replace(tzinfo=None)}
        elif ts is not None and ts > combined[sid].get("ts", ts):
            combined[sid]["ts"] = ts
        return combined[sid]

    for action, extract in _SIR_BULK_ENDPOINTS:
        count = 0
        try:
            d = _fetch_sir_bulk_json(action)
            ts = _parse_sir_bulk_meta_ts(d.get("meta", ""))
            for entry in d.get("data", []):
                sid = entry.get("IDStazione", "")
                if sid in station_ids:
                    _ensure(sid, ts).update(extract(entry))
                    count += 1
            log_scrape(f"sir_bulk:{action}", "ok", rows=count)
        except Exception as e:
            logger.error(f"SIR bulk {action} fallito: {e}")
            log_scrape(f"sir_bulk:{action}", "fail", detail=str(e))

    # Assembla record wide
    results: dict[str, dict[str, Any]] = {}
    for sid, fields in combined.items():
        ts_val = fields.pop("ts", datetime.now(UTC).replace(tzinfo=None))
        results[sid] = {
            "source": "sir_toscana",
            "station_id": sid,
            "ts": ts_val,
            "granularity": "realtime",
            **fields,
        }

    log_scrape("sir_bulk_realtime", "ok" if results else "fail", rows=len(results))
    return results

