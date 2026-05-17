"""Fetcher meteo — SIR Toscana (storico + realtime) e Netatmo real-time.

Output: righe wide (una per stazione per timestamp) compatibili con INSERT in
`observations` e `netatmo_fetch_log`.

CLI:
    uv run python -m guazza.fetchers sir-historical --station TOS01001215 --sensor termo_csv
    uv run python -m guazza.fetchers netatmo --location casa_campi
"""

from __future__ import annotations

import csv
import io
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import yaml
from dotenv import load_dotenv
from loguru import logger
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)
from tqdm import tqdm

from guazza.weights import compute_station_weight

# ── Logging strutturato ───────────────────────────────────────────────────────

def _log_scrape(scraper: str, status: str, rows: int | None = None, detail: str = "") -> None:
    """Emette un log JSON strutturato per ogni run scraper.

    Formato: {"scraper": ..., "status": "ok|fail", "ts": ..., "rows": N}
    Compatibile con AGENTS.md §Scraper fragili.
    """
    payload: dict[str, Any] = {
        "scraper": scraper,
        "status": status,
        "ts": datetime.now(tz=UTC).isoformat(),
    }
    if rows is not None:
        payload["rows"] = rows
    if detail:
        payload["detail"] = detail
    logger.info(payload)


def _is_retryable_http(exc: BaseException) -> bool:
    """Ritenta su 429 (rate limit) e 5xx. I 4xx permanenti (400, 404, 422) non si ritentano."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return True

# ── User-Agent comune ────────────────────────────────────────────────────────

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

# ── Costanti SIR ────────────────────────────────────────────────────────────

_SIR_BASE_URL = "https://www.sir.toscana.it/archivio/download.php"
_SIR_HEADERS = {"X-Requested-With": "XMLHttpRequest", "User-Agent": _UA}
_SIR_DELAY = 1.2

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


# ── Costanti Netatmo ─────────────────────────────────────────────────────────

_NETATMO_URL = "https://api.netatmo.com/api/getpublicdata"
_NETATMO_TOKEN_URL = "https://api.netatmo.com/oauth2/token"
_BBOX_PAD = 0.06
_QC_TEMP_MIN = -20.0
_QC_TEMP_MAX = 50.0
_QC_CROSS_SIGMA = 5.0
_QC_SIR_SIGMA = 8.0
_NETATMO_DELAY = 1.0

_VAR_MAP: dict[str, str] = {
    "temperature": "temp_c",
    "humidity": "humidity_pct",
    "sum_rain_1": "rain_1h",
    "rain_60min": "rain_1h",
    "wind_strength": "wind_speed_ms",
}

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ENV_FILE = _REPO_ROOT / ".env"


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
    has_flag_col: bool = schema["flag_col"]

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

        if has_flag_col and len(row) >= len(variables) + 2:
            raw_flag = row[len(variables) + 1].strip()
            row_flag = _FLAG_MAP.get(raw_flag, "ok")
        else:
            row_flag = None

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

            if has_flag_col:
                flag = row_flag if value is not None else "missing"
            else:
                flag = "ok" if value is not None else "missing"

            record[var_name] = value
            # level_m e precip_mm hanno un flag implicito: non lo salviamo in observations wide
            # perché non c'è colonna flag per variabile. Se serve, va aggiunta una colonna generica.
            # Per ora, se flag != 'ok' e value is not None, lasciamo il valore (downstream QC).
            if value is None and flag == "missing":
                record[var_name] = None

        records.append(record)

    # precip_interval_h: solo per pluvio0_24 (24h) e idro_l non ha precip
    if sensor_type == "pluvio0_24":
        for r in records:
            r["precip_interval_h"] = 24

    return records


# ═════════════════════════════════════════════════════════════════════════════
# SIR — Realtime JSON
# ═════════════════════════════════════════════════════════════════════════════

_SIR_REALTIME_BASE = "https://www.sir.toscana.it/monitoraggio"
_SIR_RT_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.sir.toscana.it/",
    "User-Agent": _UA,
}

# Formati data noti nella risposta SIR realtime
_SIR_RT_DATE_FORMATS = ["%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S"]


def _parse_sir_realtime_ts(data: dict[str, Any]) -> datetime:
    """Estrae il timestamp dalla risposta JSON SIR realtime.

    Tenta di parsare il campo "date" dal primo sensore disponibile (termo, igro, anemo).
    Fallback a now(UTC) se il campo è assente o non parsabile.
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
                return datetime.strptime(date_str, fmt).replace(tzinfo=UTC)
            except ValueError:
                continue
        logger.debug(f"SIR realtime: date non parsabile: {date_str!r}")
        break
    return datetime.now(tz=UTC)


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
    with httpx.Client(timeout=15) as client:
        r = client.get(url, params=params, headers=_SIR_RT_HEADERS)
        r.raise_for_status()
    data = r.json()

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

    # pluvio — CUM01 = cumulativo ultima ora, usato come precip_mm realtime
    if pluvio := data.get("pluvio"):
        cum01 = pluvio.get("CUM01")
        if cum01 is not None and cum01 != "-":
            try:
                record["precip_mm"] = float(cum01)
                record["precip_interval_h"] = 1
            except ValueError:
                pass

    return record


def fetch_sir_stations_realtime(
    station_ids: list[str],
    delay: float = 1.0,
) -> dict[str, dict[str, Any]]:
    """Recupera real-time per una lista di stazioni con throttling.

    Returns:
        Dict {station_id: record_wide} — le stazioni con errore vengono omesse.
    """
    results: dict[str, dict[str, Any]] = {}
    n_fail = 0
    for i, sid in enumerate(tqdm(station_ids, desc="SIR realtime", unit="staz", disable=not sys.stderr.isatty())):
        if i > 0:
            time.sleep(delay)
        try:
            results[sid] = fetch_sir_realtime(sid)
        except Exception as e:
            n_fail += 1
            logger.warning(f"SIR realtime fallito per {sid}: {e}")

    status = "ok" if results else "fail"
    detail = f"{n_fail} stazioni fallite" if n_fail else ""
    _log_scrape("sir_realtime", status, rows=len(results), detail=detail)
    return results


# ═════════════════════════════════════════════════════════════════════════════
# Netatmo — Realtime
# ═════════════════════════════════════════════════════════════════════════════


def _load_env() -> dict[str, str]:
    load_dotenv(_ENV_FILE)
    return {
        "access_token": os.getenv("NETATMO_ACCESS_TOKEN", ""),
        "refresh_token": os.getenv("NETATMO_REFRESH_TOKEN", ""),
        "client_id": os.getenv("NETATMO_CLIENT_ID", ""),
        "client_secret": os.getenv("NETATMO_CLIENT_SECRET", ""),
    }


def _refresh_token(env: dict[str, str]) -> str:
    if not env["refresh_token"] or not env["client_id"]:
        raise RuntimeError("NETATMO_REFRESH_TOKEN o NETATMO_CLIENT_ID mancanti")
    logger.info("Rinnovo access token Netatmo via refresh_token…")
    resp = httpx.post(
        _NETATMO_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": env["client_id"],
            "client_secret": env["client_secret"],
            "refresh_token": env["refresh_token"],
        },
        timeout=15,
    )
    resp.raise_for_status()
    tokens = resp.json()
    new_access = tokens["access_token"]
    new_refresh = tokens.get("refresh_token", env["refresh_token"])
    if _ENV_FILE.exists():
        content = _ENV_FILE.read_text()
        content = re.sub(r"NETATMO_ACCESS_TOKEN=\S*", f"NETATMO_ACCESS_TOKEN={new_access}", content)
        content = re.sub(r"NETATMO_REFRESH_TOKEN=\S*", f"NETATMO_REFRESH_TOKEN={new_refresh}", content)
        _ENV_FILE.write_text(content)
    logger.info("Token rinnovato e salvato nel .env")
    return str(new_access)


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (401, 403):
        return False
    return True


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception(_is_retryable),
)
def _fetch_public_data(token: str, lat: float, lon: float) -> list[dict[str, Any]]:
    resp = httpx.get(
        _NETATMO_URL,
        params={
            "lat_ne": lat + _BBOX_PAD,
            "lon_ne": lon + _BBOX_PAD,
            "lat_sw": lat - _BBOX_PAD,
            "lon_sw": lon - _BBOX_PAD,
            "filter": "true",
        },
        headers={"Authorization": f"Bearer {token}"},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "ok":
        raise ValueError(f"API error: {data}")
    return data.get("body", [])  # type: ignore[no-any-return]


def _call_with_refresh(env: dict[str, str], lat: float, lon: float) -> list[dict[str, Any]]:
    token = env["access_token"]
    try:
        return _fetch_public_data(token, lat, lon)
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (401, 403):
            token = _refresh_token(env)
            env["access_token"] = token
            return _fetch_public_data(token, lat, lon)
        raise


def _extract_measures(measures: dict[str, Any]) -> dict[str, float | None]:
    result: dict[str, float | None] = {
        "temp_c": None,
        "humidity_pct": None,
        "rain_1h": None,
        "wind_speed_ms": None,
    }
    for _mac, mdata in measures.items():
        types: list[str] = mdata.get("type", [])
        res: dict[str, list[float]] = mdata.get("res", {})
        if not res:
            continue
        values = next(iter(res.values()))
        for i, vtype in enumerate(types):
            if i >= len(values):
                continue
            internal = _VAR_MAP.get(vtype)
            if internal and result[internal] is None:
                result[internal] = float(values[i])
    return result


def _measure_ts(measures: dict[str, Any]) -> datetime:
    for mdata in measures.values():
        for ts_str in mdata.get("res", {}):
            try:
                return datetime.fromtimestamp(int(ts_str), tz=UTC)
            except (ValueError, OSError):
                pass
    return datetime.now(tz=UTC)


def _qc_range(temp: float | None, humidity: float | None) -> bool:
    if temp is not None and not (_QC_TEMP_MIN <= temp <= _QC_TEMP_MAX):
        return False
    if humidity is not None and not (0.0 <= humidity <= 100.0):
        return False
    return True


def _get_recent_sir_temp(db: Any, location_id: str, max_age_min: int = 60) -> float | None:
    """Legge l'ultima temperatura SIR da observations (entro max_age_min minuti)."""
    try:
        row = db.execute(
            f"""
            SELECT temp_c FROM observations
            WHERE source = 'sir_toscana'
              AND location_id = ?
              AND ts >= (CURRENT_TIMESTAMP - INTERVAL '{max_age_min} minutes')
            ORDER BY ts DESC
            LIMIT 1
            """,
            [location_id],
        ).fetchone()
        return float(row[0]) if row else None
    except Exception as exc:
        logger.debug(f"[{location_id}] _get_recent_sir_temp: {exc}")
        return None


def _apply_sir_qc(stations: list[Any], sir_temp: float, threshold_c: float = _QC_SIR_SIGMA) -> None:
    for sd in stations:
        t = sd.measures.get("temp_c")
        if t is not None:
            sd.qc_sir = abs(t - sir_temp) <= threshold_c


@dataclass
class _StationData:
    mac: str
    lat: float
    lon: float
    alt_m: int | None
    distance_km: float
    delta_elev_m: float
    weight: float
    measures: dict[str, float | None]
    ts: datetime
    qc_range: bool = True
    qc_cross: bool = True
    qc_sir: bool = True

    @property
    def qc_pass(self) -> bool:
        return self.qc_range and self.qc_cross and self.qc_sir


def fetch_netatmo_location(
    location_id: str,
    loc: dict[str, Any],
    env: dict[str, str],
    db: Any | None = None,
) -> list[_StationData]:
    """Chiama getpublicdata per una location e restituisce le stazioni processate."""
    target_lat: float = loc["lat"]
    target_lon: float = loc["lon"]
    target_elev: float = loc["elevation_m"]

    raw_stations = _call_with_refresh(env, target_lat, target_lon)
    logger.info(f"[{location_id}] getpublicdata → {len(raw_stations)} stazioni nell'area")

    results: list[_StationData] = []
    for s in raw_stations:
        place = s.get("place", {})
        location = place.get("location", [None, None])
        s_lon = location[0]
        s_lat = location[1]
        if s_lat is None or s_lon is None:
            continue

        s_alt = place.get("altitude")
        s_elev = float(s_alt) if s_alt is not None else target_elev

        weight, dist_km, delta_elev = compute_station_weight(
            float(s_lat), float(s_lon), s_elev,
            target_lat, target_lon, target_elev,
            "netatmo",
        )

        m = _extract_measures(s.get("measures", {}))
        ts = _measure_ts(s.get("measures", {}))

        sd = _StationData(
            mac=s["_id"],
            lat=round(float(s_lat), 6),
            lon=round(float(s_lon), 6),
            alt_m=int(s_alt) if s_alt is not None else None,
            distance_km=dist_km,
            delta_elev_m=delta_elev,
            weight=weight,
            measures=m,
            ts=ts,
            qc_range=_qc_range(m.get("temp_c"), m.get("humidity_pct")),
        )
        results.append(sd)

    # Cross-validation temperatura
    valid_temps_list = sorted(
        sd.measures["temp_c"]
        for sd in results
        if sd.qc_range and sd.measures["temp_c"] is not None
    )
    if valid_temps_list:
        mid = len(valid_temps_list) // 2
        t_ref = (
            valid_temps_list[mid]
            if len(valid_temps_list) % 2 == 1
            else (valid_temps_list[mid - 1] + valid_temps_list[mid]) / 2
        )
        for sd in results:
            t = sd.measures.get("temp_c")
            if t is not None:
                sd.qc_cross = abs(t - t_ref) <= _QC_CROSS_SIGMA

    if db is not None:
        sir_temp = _get_recent_sir_temp(db, location_id)
        if sir_temp is not None:
            _apply_sir_qc(results, sir_temp)
            n_sir_fail = sum(1 for sd in results if not sd.qc_sir)
            logger.info(
                f"[{location_id}] QC SIR (T_ref={sir_temp:.1f}°C): {n_sir_fail} stazioni escluse"
            )

    n_ok = sum(1 for sd in results if sd.qc_pass)
    n_fail = len(results) - n_ok
    logger.info(f"[{location_id}] QC finale: {n_ok} OK, {n_fail} escluse")
    return results


def save_netatmo_to_db(
    db: Any,
    location_id: str,
    stations: list[_StationData],
    fetched_at: datetime,
) -> None:
    """Scrive in netatmo_fetch_log e observations (wide, una riga per stazione)."""
    if not stations:
        logger.warning(f"[{location_id}] Nessuna stazione da salvare")
        return

    # netatmo_fetch_log
    db.executemany(
        """
        INSERT INTO netatmo_fetch_log
            (fetched_at, location_id, station_id, lat, lon, alt_m,
             distance_km, delta_elev_m, weight, temperature, humidity, rain_1h, wind_speed)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (fetched_at, location_id, station_id) DO NOTHING
        """,
        [
            [
                fetched_at, location_id, sd.mac,
                sd.lat, sd.lon, sd.alt_m,
                sd.distance_km, sd.delta_elev_m, sd.weight,
                sd.measures.get("temp_c"),
                sd.measures.get("humidity_pct"),
                sd.measures.get("rain_1h"),
                sd.measures.get("wind_speed_ms"),
            ]
            for sd in stations
        ],
    )

    # observations — wide, una riga per stazione (PK source+station_id+ts)
    obs_rows: list[list[Any]] = []
    for sd in stations:
        obs_rows.append([
            "netatmo", sd.mac, location_id,
            sd.ts,
            "realtime",  # granularity
            sd.measures.get("temp_c"),
            None,  # tmin_c
            None,  # tmax_c
            sd.measures.get("humidity_pct"),
            sd.measures.get("rain_1h"),
            1 if sd.measures.get("rain_1h") is not None else None,  # precip_interval_h
            sd.measures.get("wind_speed_ms"),
            None,  # wind_dir_deg
            None,  # wind_gust_ms
            None,  # pressure_hpa
            None,  # level_m
            None,  # pm10
            None,  # pm25
            None,  # no2
            None,  # o3
            sd.weight,
            sd.qc_pass,
        ])

    if obs_rows:
        db.executemany(
            """
            INSERT INTO observations
                (source, station_id, location_id, ts, granularity,
                 temp_c, tmin_c, tmax_c, humidity_pct, precip_mm, precip_interval_h,
                 wind_speed_ms, wind_dir_deg, wind_gust_ms, pressure_hpa, level_m,
                 pm10_ugm3, pm25_ugm3, no2_ugm3, o3_ugm3,
                 weight, qc_pass)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (source, station_id, ts, granularity) DO UPDATE SET
                location_id = excluded.location_id,
                temp_c        = excluded.temp_c,
                humidity_pct  = excluded.humidity_pct,
                precip_mm     = excluded.precip_mm,
                precip_interval_h = excluded.precip_interval_h,
                wind_speed_ms = excluded.wind_speed_ms,
                weight        = excluded.weight,
                qc_pass       = excluded.qc_pass
            """,
            obs_rows,
        )

    logger.info(
        f"[{location_id}] Salvate {len(stations)} stazioni in netatmo_fetch_log, "
        f"{len(obs_rows)} osservazioni in observations"
    )
    _log_scrape(f"netatmo:{location_id}", "ok", rows=len(obs_rows))


# ═════════════════════════════════════════════════════════════════════════════
# Open-Meteo — Forecast + Historical Forecast
# ═════════════════════════════════════════════════════════════════════════════

_OM_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_OM_HISTORICAL_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"

# Variabili orarie richieste
_OM_HOURLY_VARS = [
    "temperature_2m",
    "relative_humidity_2m",
    "precipitation",
    "wind_speed_10m",
    "wind_direction_10m",
    "wind_gusts_10m",
    "surface_pressure",
]

# Mapping variabile Open-Meteo → colonna observations wide
_OM_VAR_MAP: dict[str, str] = {
    "temperature_2m": "temp_c",
    "relative_humidity_2m": "humidity_pct",
    "precipitation": "precip_mm",
    "wind_speed_10m": "wind_speed_ms",
    "wind_direction_10m": "wind_dir_deg",
    "wind_gusts_10m": "wind_gust_ms",
    "surface_pressure": "pressure_hpa",
}

# Cadenza run per modello (ore UTC). Usata per arrotondare ts_run.
_MODEL_RUN_HOURS: dict[str, list[int]] = {
    "ecmwf_ifs":                    [0, 6, 12, 18],
    "ecmwf_ifs025":                 [0, 6, 12, 18],
    "icon_eu":                      [0, 3, 6, 9, 12, 15, 18, 21],
    "icon_d2":                      [0, 3, 6, 9, 12, 15, 18, 21],
    "gfs025":                       [0, 6, 12, 18],
    "arome_france":                 [0, 3, 6, 9, 12, 15, 18, 21],
    "italia_meteo_arpae_icon_2i":   [0, 12],
    # fallback generico
    "default": [0, 6, 12, 18],
}

# Modelli disponibili per l'area Toscana
_OM_MODELS: list[str] = [
    "ecmwf_ifs",
    "icon_eu",
    "icon_d2",
    "gfs025",
    "arome_france",
    "italia_meteo_arpae_icon_2i",  # ItaliaMeteo/ARPAE, 2.2km Italia, 72h, dati assimilati italiani
]


def _infer_ts_run(model: str, now_utc: datetime) -> datetime:
    """Stima ts_run = ultimo run completato prima di now_utc per il modello dato.

    Arrotonda per difetto all'ora UTC più recente nella lista dei run del modello.
    I run NWP hanno tipicamente un lag di ~2-4h dalla cutoff, ma qui usiamo
    l'ora nominale del run (es. ECMWF 00 UTC) perché è quella che identifica
    il run nei dati archiviati.
    """
    run_hours = _MODEL_RUN_HOURS.get(model, _MODEL_RUN_HOURS["default"])
    current_hour = now_utc.hour
    # trova l'ultima ora di run ≤ ora corrente
    last_run_hour = max((h for h in run_hours if h <= current_hour), default=run_hours[-1])
    if last_run_hour > current_hour:
        # tutti i run sono dopo l'ora corrente: prendi l'ultimo run del giorno precedente
        ts_run = (now_utc - timedelta(days=1)).replace(
            hour=run_hours[-1], minute=0, second=0, microsecond=0
        )
    else:
        ts_run = now_utc.replace(hour=last_run_hour, minute=0, second=0, microsecond=0)
    return ts_run


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=15))
def _fetch_om_json(url: str, params: dict[str, str | int | float | list[str]]) -> dict[str, Any]:
    logger.debug(f"Open-Meteo fetch: {url} params={params}")
    with httpx.Client(timeout=30) as client:
        r = client.get(url, params=params)
        r.raise_for_status()
    return r.json()  # type: ignore[no-any-return]


def _wait_historical(retry_state: RetryCallState) -> float:
    """Rispetta Retry-After dal 429; fallback exponential backoff."""
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
        retry_after = exc.response.headers.get("Retry-After")
        if retry_after:
            try:
                return float(retry_after) + 1.0
            except ValueError:
                pass
        return 60.0
    # exponential backoff per 5xx e altri errori
    return min(60.0, 5.0 * (2.0 ** (retry_state.attempt_number - 1)))


def _log_http_error(retry_state: RetryCallState) -> None:
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    if isinstance(exc, httpx.HTTPStatusError):
        body = exc.response.text[:200].replace("\n", " ")
        logger.warning(
            f"Open-Meteo historical HTTP {exc.response.status_code} "
            f"(attempt {retry_state.attempt_number}): {exc.request.url} — {body}"
        )
    else:
        logger.warning(f"Open-Meteo historical retry (attempt {retry_state.attempt_number}): {exc}")


@retry(
    stop=stop_after_attempt(5),
    wait=_wait_historical,
    retry=retry_if_exception(_is_retryable_http),
    before_sleep=_log_http_error,
)
def _fetch_om_json_historical(url: str, params: dict[str, str | int | float | list[str]]) -> dict[str, Any]:
    logger.debug(f"Open-Meteo historical fetch: {url} params={params}")
    with httpx.Client(timeout=90, headers={"User-Agent": _UA}) as client:
        r = client.get(url, params=params)
        r.raise_for_status()
    return r.json()  # type: ignore[no-any-return]


def _parse_om_response(
    data: dict[str, Any],
    model: str,
    location_id: str,
    ts_run: datetime | None = None,
) -> list[dict[str, Any]]:
    """Converte risposta Open-Meteo in lista di record wide per tabella forecasts.

    Args:
        ts_run: se None (modalità storica), viene inferita per ogni riga con
                _infer_ts_run(model, ts_valid). Se fornita (modalità live),
                viene usata fissa per tutti i record.

    Returns:
        Lista di dict con chiavi: source, location_id, ts_run, ts_valid,
        lead_time_h + colonne meteo.
    """
    hourly = data.get("hourly", {})
    times: list[str] = hourly.get("time", [])
    if not times:
        logger.warning(f"Open-Meteo [{location_id}] [{model}] risposta senza dati hourly")
        return []

    records: list[dict[str, Any]] = []
    for i, time_str in enumerate(times):
        try:
            # ISO8601 senza timezone: l'API restituisce UTC quando timezone=UTC
            ts_valid = datetime.fromisoformat(time_str).replace(tzinfo=UTC)
        except ValueError:
            logger.debug(f"ts_valid non parsabile: {time_str!r}")
            continue

        # Modalità storica: ts_run inferita per ogni ora (ogni record può avere ts_run diversa)
        effective_ts_run = ts_run if ts_run is not None else _infer_ts_run(model, ts_valid)
        lead_h = int((ts_valid - effective_ts_run).total_seconds() / 3600)

        rec: dict[str, Any] = {
            "source": f"open_meteo_{model}",
            "location_id": location_id,
            "ts_run": effective_ts_run,
            "ts_valid": ts_valid,
            "lead_time_h": lead_h,
        }
        for om_var, col in _OM_VAR_MAP.items():
            series = hourly.get(om_var, [])
            val = series[i] if i < len(series) else None
            rec[col] = float(val) if val is not None else None

        records.append(rec)

    logger.debug(
        f"Open-Meteo [{location_id}] [{model}] → {len(records)} righe"
        + (f" (ts_run={ts_run})" if ts_run is not None else " (ts_run=inferita per riga)")
    )
    return records


def fetch_openmeteo_forecast(
    location_id: str,
    lat: float,
    lon: float,
    models: list[str] | None = None,
    forecast_days: int = 7,
    now_utc: datetime | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Fetch forecast live da Open-Meteo per una location, multi-modello.

    Args:
        location_id: ID location Guazza.
        lat, lon: coordinate della location.
        models: lista modelli (default: tutti i modelli _OM_MODELS).
        forecast_days: giorni di forecast (1–16).
        now_utc: timestamp corrente UTC (iniettabile per test).

    Returns:
        Dict {model: [record_wide, ...]} — un record per ora per modello.
    """
    if models is None:
        models = _OM_MODELS
    if now_utc is None:
        now_utc = datetime.now(tz=UTC)

    results: dict[str, list[dict[str, Any]]] = {}

    for model in tqdm(models, desc=f"OM forecast [{location_id}]", unit="model", disable=not sys.stderr.isatty()):
        ts_run = _infer_ts_run(model, now_utc)
        params: dict[str, str | int | float | list[str]] = {
            "latitude": lat,
            "longitude": lon,
            "hourly": ",".join(_OM_HOURLY_VARS),
            "models": model,
            "forecast_days": forecast_days,
            "timezone": "UTC",
            "wind_speed_unit": "ms",
        }
        try:
            data = _fetch_om_json(_OM_FORECAST_URL, params)
            records = _parse_om_response(data, model, location_id, ts_run)
            results[model] = records
            _log_scrape(f"openmeteo_forecast:{location_id}:{model}", "ok", rows=len(records))
        except Exception as e:
            logger.error(f"Open-Meteo forecast [{location_id}] [{model}] fallito: {e}")
            _log_scrape(f"openmeteo_forecast:{location_id}:{model}", "fail", detail=str(e))
            results[model] = []
        time.sleep(0.5)  # throttle gentile tra modelli

    return results


def fetch_openmeteo_historical(
    location_id: str,
    lat: float,
    lon: float,
    start_date: str,
    end_date: str,
    models: list[str] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Fetch storico forecast da Open-Meteo Historical Forecast API.

    Usata per backfill training set (dati 2022+).
    ts_run viene inferita per ogni riga con _infer_ts_run(model, ts_valid):
    ogni ora ha il suo ts_run = ultimo run nominale per difetto.

    Args:
        start_date, end_date: formato "YYYY-MM-DD".

    Returns:
        Dict {model: [record_wide, ...]}
    """
    if models is None:
        models = _OM_MODELS

    results: dict[str, list[dict[str, Any]]] = {}

    for model in tqdm(models, desc=f"OM historical [{location_id}]", unit="model", disable=not sys.stderr.isatty()):
        params: dict[str, str | int | float | list[str]] = {
            "latitude": lat,
            "longitude": lon,
            "hourly": ",".join(_OM_HOURLY_VARS),
            "models": model,
            "start_date": start_date,
            "end_date": end_date,
            "timezone": "UTC",
            "wind_speed_unit": "ms",
        }
        try:
            data = _fetch_om_json(_OM_HISTORICAL_URL, params)
            # ts_run=None → inferita per ogni riga in _parse_om_response
            records = _parse_om_response(data, model, location_id, ts_run=None)
            results[model] = records
            _log_scrape(
                f"openmeteo_historical:{location_id}:{model}",
                "ok",
                rows=len(records),
                detail=f"{start_date} to {end_date}",
            )
        except Exception as e:
            logger.error(f"Open-Meteo historical [{location_id}] [{model}] fallito: {e}")
            _log_scrape(f"openmeteo_historical:{location_id}:{model}", "fail", detail=str(e))
            results[model] = []
        time.sleep(0.5)

    return results


def fetch_openmeteo_forecast_batch(
    locations: dict[str, dict[str, Any]],
    models: list[str] | None = None,
    forecast_days: int = 7,
    now_utc: datetime | None = None,
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Fetch forecast live da Open-Meteo per multiple location in batch.

    Sfrutta la capacità di Open-Meteo di ricevere più coordinate in una chiamata.
    Riduce il numero di round-trip HTTP e il throttling.

    Returns:
        Dict {location_id: {model: [record_wide, ...]}}
    """
    if models is None:
        models = _OM_MODELS
    if now_utc is None:
        now_utc = datetime.now(tz=UTC)

    # Inizializza risultati: {loc_id: {model: []}}
    results: dict[str, dict[str, list[dict[str, Any]]]] = {
        loc_id: {model: [] for model in models} for loc_id in locations
    }

    # Ordine stabile per le location
    loc_ids = sorted(locations.keys())
    lats = [locations[lid]["lat"] for lid in loc_ids]
    lons = [locations[lid]["lon"] for lid in loc_ids]

    for model in tqdm(models, desc="OM forecast batch", unit="model", disable=not sys.stderr.isatty()):
        ts_run = _infer_ts_run(model, now_utc)
        params: dict[str, str | int | float | list[str]] = {
            "latitude": ",".join(map(str, lats)),
            "longitude": ",".join(map(str, lons)),
            "hourly": ",".join(_OM_HOURLY_VARS),
            "models": model,
            "forecast_days": forecast_days,
            "timezone": "UTC",
            "wind_speed_unit": "ms",
        }
        try:
            data = _fetch_om_json(_OM_FORECAST_URL, params)

            # Se abbiamo più coordinate, Open-Meteo restituisce una LISTA di oggetti
            if isinstance(data, list):
                responses = data
            else:
                responses = [data]

            for lid, resp in zip(loc_ids, responses, strict=True):
                records = _parse_om_response(resp, model, lid, ts_run)
                results[lid][model] = records
                _log_scrape(f"openmeteo_forecast:{lid}:{model}", "ok", rows=len(records))

        except Exception as e:
            logger.error(f"Open-Meteo forecast batch [{model}] fallito: {e}")
            for lid in loc_ids:
                _log_scrape(f"openmeteo_forecast:{lid}:{model}", "fail", detail=str(e))

        time.sleep(0.5)

    return results


def _fetch_one_model_historical(
    model: str,
    chunks: list[tuple[str, str]],
    loc_ids: list[str],
    lats: list[float],
    lons: list[float],
    results: dict[str, dict[str, list[dict[str, Any]]]],
) -> None:
    """Fetch storico per un singolo modello su tutti i chunk.

    Scrive direttamente su results[lid][model] — thread-safe perche
    ogni modello ha la propria chiave nel dict annidato.
    """
    for c_start, c_end in chunks:
        params: dict[str, str | int | float | list[str]] = {
            "latitude": ",".join(map(str, lats)),
            "longitude": ",".join(map(str, lons)),
            "hourly": ",".join(_OM_HOURLY_VARS),
            "models": model,
            "start_date": c_start,
            "end_date": c_end,
            "timezone": "UTC",
            "wind_speed_unit": "ms",
        }
        try:
            data = _fetch_om_json_historical(_OM_HISTORICAL_URL, params)
            responses = data if isinstance(data, list) else [data]

            for lid, resp in zip(loc_ids, responses, strict=True):
                records = _parse_om_response(resp, model, lid, ts_run=None)
                results[lid][model].extend(records)
                _log_scrape(
                    f"openmeteo_historical:{lid}:{model}",
                    "ok",
                    rows=len(records),
                    detail=f"{c_start} to {c_end}",
                )
        except Exception as e:
            logger.error(f"Open-Meteo historical batch [{model}] [{c_start}→{c_end}] fallito: {e}")
            for lid in loc_ids:
                _log_scrape(f"openmeteo_historical:{lid}:{model}", "fail", detail=str(e))

        time.sleep(3.0)


def fetch_openmeteo_historical_batch(
    locations: dict[str, Any],
    start_date: str,
    end_date: str,
    models: list[str] | None = None,
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Fetch storico forecast da Open-Meteo Historical Forecast API in batch.

    I modelli vengono fetchati in parallelo con ThreadPoolExecutor(3).
    Ogni modello divide la propria richiesta in chunk temporali per evitare
    timeout lato server: modelli ad alta risoluzione (icon_d2, arome_france)
    usano chunk da 90gg; altri (ecmwf_ifs, icon_eu, gfs025)
    usano 180gg.

    Returns:
        Dict {location_id: {model: [record_wide, ...]}}
    """
    if models is None:
        models = _OM_MODELS

    _HIGH_RES = {"icon_d2", "arome_france", "italia_meteo_arpae_icon_2i"}
    _DEFAULT_CHUNK = 180
    _HR_CHUNK = 90

    results: dict[str, dict[str, list[dict[str, Any]]]] = {
        loc_id: {model: [] for model in models} for loc_id in locations
    }

    loc_ids = sorted(locations.keys())
    lats = [locations[lid]["lat"] for lid in loc_ids]
    lons = [locations[lid]["lon"] for lid in loc_ids]

    # Prepara chunk per modello
    model_chunks: dict[str, list[tuple[str, str]]] = {}
    for model in models:
        chunk_days = _HR_CHUNK if model in _HIGH_RES else _DEFAULT_CHUNK
        start_dt = datetime.strptime(start_date, "%Y-%m-%d").date()
        end_dt = datetime.strptime(end_date, "%Y-%m-%d").date()
        chunks: list[tuple[str, str]] = []
        curr_start = start_dt
        while curr_start <= end_dt:
            curr_end = min(curr_start + timedelta(days=chunk_days - 1), end_dt)
            chunks.append((curr_start.isoformat(), curr_end.isoformat()))
            curr_start = curr_end + timedelta(days=1)
        model_chunks[model] = chunks

    _tty = sys.stderr.isatty()
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(
                _fetch_one_model_historical,
                model, model_chunks[model], loc_ids, lats, lons, results,
            ): model
            for model in models
        }
        for future in tqdm(
            as_completed(futures),
            total=len(futures),
            desc="OM historical batch",
            unit="model",
            disable=not _tty,
        ):
            # Propaga eccezioni — se un modello fallisce, tutto fallisce
            future.result()

    return results


def fetch_openmeteo_all_locations(
    locations: dict[str, Any],
    models: list[str] | None = None,
    forecast_days: int = 7,
    now_utc: datetime | None = None,
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Fetch forecast per tutte le location (usa il batching)."""
    return fetch_openmeteo_forecast_batch(
        locations=locations,
        models=models,
        forecast_days=forecast_days,
        now_utc=now_utc,
    )


def fetch_netatmo_all_locations(
    db: Any,
    locations: dict[str, Any] | None = None,
    target_location: str | None = None,
) -> dict[str, list[_StationData]]:
    """Fetch Netatmo per tutte le location (o solo target_location se specificata)."""
    env = _load_env()
    if not env["access_token"] and not env["refresh_token"]:
        raise RuntimeError("NETATMO_ACCESS_TOKEN e NETATMO_REFRESH_TOKEN mancanti nel .env")

    if locations is None:
        config_dir = Path(os.environ.get("CONFIG_DIR", str(_REPO_ROOT / "config")))
        with (config_dir / "locations.yaml").open() as f:
            locations = yaml.safe_load(f)["locations"]

    fetched_at = datetime.now(tz=UTC)
    results: dict[str, list[_StationData]] = {}

    active_locs = {k: v for k, v in locations.items() if not target_location or k == target_location}
    for loc_id, loc in tqdm(active_locs.items(), desc="Netatmo", unit="loc", disable=not sys.stderr.isatty()):
        try:
            stations = fetch_netatmo_location(loc_id, loc, env, db=db)
            save_netatmo_to_db(db, loc_id, stations, fetched_at)
            results[loc_id] = stations
        except Exception as e:
            logger.error(f"[{loc_id}] Fetch fallito: {e}")
            _log_scrape(f"netatmo:{loc_id}", "fail", detail=str(e))
            results[loc_id] = []
        if not target_location:
            time.sleep(_NETATMO_DELAY)

    return results


# ═════════════════════════════════════════════════════════════════════════════
# ARPAT — Qualità aria (NRT orario + bollettini giornalieri)
# ═════════════════════════════════════════════════════════════════════════════

_ARPAT_NRT_URL = "https://api.arpat.toscana.it/app/air/nrt/valori_last"
_ARPAT_BOLLETTINI_URL = "https://api.arpat.toscana.it/app/air/bollettini/dati"

_arpat_nrt_first_call_logged = False

# Mapping nome variabile ARPAT → colonna observations wide (None = non in schema, ignorato)
_ARPAT_NRT_VAR_MAP: dict[str, str | None] = {
    "NO2":     "no2_ugm3",
    "O3":      "o3_ugm3",
    "CO":      None,   # non in schema wide — ignorato (CO non in observations)
    "BENZENE": None,   # non in schema wide — ignorato
}

_ARPAT_BOLL_VAR_MAP: dict[str, str | None] = {
    "PM10":    "pm10_ugm3",
    "PM2.5":   "pm25_ugm3",
    "NO2":     "no2_ugm3",
    "O3":      "o3_ugm3",
    "CO":      None,
    "BENZENE": None,
}


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=60, max=600),
    retry=retry_if_exception(lambda e: not isinstance(e, ValueError)),
)
def _fetch_arpat_json(url: str, params: dict[str, str] | None = None) -> Any:
    """Fetch JSON da endpoint ARPAT con retry (backoff 60s/300s/600s)."""
    with httpx.Client(timeout=30, headers={"User-Agent": _UA}) as client:
        r = client.get(url, params=params)
        r.raise_for_status()
    return r.json()


def fetch_arpat_nrt(
    location_id: str,
    arpat_stations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Fetch valori NRT orari ARPAT (NO2, O3) per una location.

    La risposta reale è {"items": [{"stazione": "FI-SIGNA", "inquinante": "NO2",
    "valore": 3, "data_ora_osservazione": "2026-05-15T15:00", ...}, ...]}.
    Una riga per (stazione, inquinante) — aggrega per stazione prima di costruire
    il record wide.

    Args:
        location_id: ID location Guazza.
        arpat_stations: lista di {"id": str, "weight": float} da locations.yaml.

    Returns:
        Lista di record wide compatibili con upsert su `observations`.
        Una riga per stazione ARPAT con granularity='hourly'.
    """
    try:
        data = _fetch_arpat_json(_ARPAT_NRT_URL)
    except Exception as e:
        logger.error(f"ARPAT NRT [{location_id}] fetch fallito: {e}")
        _log_scrape(f"arpat_nrt:{location_id}", "fail", detail=str(e))
        return []

    global _arpat_nrt_first_call_logged
    if not _arpat_nrt_first_call_logged:
        logger.debug(f"ARPAT NRT raw response (first 200): {str(data)[:200]}")
        _arpat_nrt_first_call_logged = True

    # Risposta reale: {"items": [{"stazione": ..., "inquinante": ..., "valore": ...,
    #   "data_ora_osservazione": ..., "unita_di_misura": ...}, ...]}
    # Aggrega per stazione: {station_id: {inquinante: (valore, ts)}}
    raw_items: list[Any] = []
    if isinstance(data, list):
        raw_items = data
    elif isinstance(data, dict):
        raw_items = data.get("items") or data.get("stazioni") or data.get("data") or []

    # {station_id_upper: {inquinante_upper: (valore, ts_str)}}
    by_station: dict[str, dict[str, tuple[Any, str]]] = {}
    for item in raw_items:
        sid = str(item.get("stazione") or item.get("codice_stazione") or item.get("id") or "").upper()
        inq = str(item.get("inquinante") or "").upper()
        if not sid or not inq:
            continue
        val = item.get("valore")
        ts_str = str(item.get("data_ora_osservazione") or item.get("data") or item.get("timestamp") or "")
        if sid not in by_station:
            by_station[sid] = {}
        by_station[sid][inq] = (val, ts_str)

    now_utc = datetime.now(tz=UTC).replace(minute=0, second=0, microsecond=0)
    records: list[dict[str, Any]] = []

    for st in arpat_stations:
        station_id = str(st["id"]).upper()
        weight = float(st.get("weight", 1.0))
        inq_map = by_station.get(station_id)
        if inq_map is None:
            logger.debug(f"ARPAT NRT: stazione {station_id} non trovata nella risposta")
            continue

        # Timestamp: usa il più recente tra gli inquinanti disponibili
        ts: datetime = now_utc
        for _inq, (_, ts_str) in inq_map.items():
            if ts_str:
                try:
                    parsed = datetime.fromisoformat(ts_str.replace(" ", "T"))
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=UTC)
                    if parsed > ts or ts is now_utc:
                        ts = parsed
                except ValueError:
                    pass

        rec: dict[str, Any] = {
            "source": "arpat",
            "station_id": station_id,
            "location_id": location_id,
            "ts": ts,
            "granularity": "hourly",
            "weight": weight,
            "qc_pass": True,
        }
        for arpat_var, col in _ARPAT_NRT_VAR_MAP.items():
            if col is None:
                continue
            entry = inq_map.get(arpat_var.upper())
            raw = entry[0] if entry is not None else None
            try:
                rec[col] = float(raw) if raw is not None else None
            except (TypeError, ValueError):
                rec[col] = None

        records.append(rec)

    _log_scrape(f"arpat_nrt:{location_id}", "ok", rows=len(records))
    return records


def fetch_arpat_bollettini(
    location_id: str,
    arpat_stations: list[dict[str, Any]],
    date: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch bollettino giornaliero ARPAT (PM10, PM2.5, NO2) per una location.

    La risposta reale è {"items": [{"data_osservazione": "2026-05-14",
    "stazione": "PO-ROMA", "inquinante": "PM10", "valore": "21", ...}, ...]}.
    Una riga per (data, stazione, inquinante) — aggrega per inquinante per
    costruire il record wide.

    Args:
        location_id: ID location Guazza.
        arpat_stations: lista di {"id": str, "weight": float} da locations.yaml.
        date: data target YYYY-MM-DD (default: ieri).

    Returns:
        Lista di record wide compatibili con upsert su `observations`.
        Una riga per stazione ARPAT con granularity='daily'.
    """
    if date is None:
        date = (datetime.now(tz=UTC) - timedelta(days=1)).strftime("%Y-%m-%d")

    # Validazione data prima del fetch — evita chiamate HTTP inutili
    try:
        ts_day = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError as e:
        raise ValueError(f"date deve essere YYYY-MM-DD, ricevuto: {date!r}") from e

    records: list[dict[str, Any]] = []
    n_fail = 0

    for st in tqdm(arpat_stations, desc="ARPAT bollettini", unit="staz", disable=not sys.stderr.isatty()):
        station_id = str(st["id"]).upper()
        weight = float(st.get("weight", 1.0))

        try:
            data = _fetch_arpat_json(
                _ARPAT_BOLLETTINI_URL,
                params={
                    "startdate": date,
                    "enddate": date,
                    "stazione": station_id,
                    "limit": "1000",
                },
            )
        except Exception as e:
            n_fail += 1
            logger.warning(
                f"ARPAT bollettini [{location_id}] stazione {station_id} fallito: {e}"
            )
            continue

        # Risposta: {"items": [{"data_osservazione": ..., "inquinante": ..., "valore": ...}]}
        # Una riga per inquinante — aggrega in dict {inquinante_upper: valore}
        raw_items: list[Any] = []
        if isinstance(data, list):
            raw_items = data
        elif isinstance(data, dict):
            raw_items = data.get("items") or data.get("stazioni") or data.get("data") or []

        inq_map: dict[str, Any] = {}
        for item in raw_items:
            inq = str(item.get("inquinante") or "").upper()
            val = item.get("valore")
            if inq:
                inq_map[inq] = val

        if not inq_map:
            logger.debug(f"ARPAT bollettini: nessun dato per stazione {station_id} data {date}")
            continue

        rec: dict[str, Any] = {
            "source": "arpat",
            "station_id": station_id,
            "location_id": location_id,
            "ts": ts_day,
            "granularity": "daily",
            "weight": weight,
            "qc_pass": True,
        }
        for arpat_var, col in _ARPAT_BOLL_VAR_MAP.items():
            if col is None:
                continue
            raw = inq_map.get(arpat_var.upper())
            try:
                rec[col] = float(raw) if raw is not None else None
            except (TypeError, ValueError):
                rec[col] = None

        records.append(rec)

    status = "fail" if (not records and n_fail) else "ok"
    detail = f"{n_fail} stazioni fallite" if n_fail else ""
    _log_scrape(f"arpat_bollettini:{location_id}", status, rows=len(records), detail=detail)
    return records


def _fetch_one_arpat_bollettini_station(
    st: dict[str, Any],
    location_id: str,
    start_date: str,
    end_date: str,
) -> list[dict[str, Any]]:
    """Fetch e parse bollettini ARPAT per una singola stazione.

    Returns: lista di record wide, vuota se fallimento.
    """
    station_id = str(st["id"]).upper()
    weight = float(st.get("weight", 1.0))

    try:
        data = _fetch_arpat_json(
            _ARPAT_BOLLETTINI_URL,
            params={
                "startdate": start_date,
                "enddate": end_date,
                "stazione": station_id,
                "limit": "100000",
            },
        )
    except Exception as e:
        logger.warning(
            f"ARPAT bollettini range [{location_id}] stazione {station_id} fallito: {e}"
        )
        _log_scrape(
            f"arpat_bollettini_range:{location_id}:{station_id}",
            "fail",
            detail=str(e),
        )
        return []

    raw_items: list[Any] = []
    if isinstance(data, list):
        raw_items = data
    elif isinstance(data, dict):
        raw_items = data.get("items") or data.get("stazioni") or data.get("data") or []

    by_date: dict[str, dict[str, Any]] = {}
    for item in raw_items:
        date_str = str(
            item.get("data_osservazione")
            or item.get("data")
            or item.get("date")
            or ""
        ).strip()
        inq = str(item.get("inquinante") or "").upper()
        val = item.get("valore")
        if not date_str or not inq:
            continue
        if date_str not in by_date:
            by_date[date_str] = {}
        by_date[date_str][inq] = val

    records: list[dict[str, Any]] = []
    for date_str, inq_map in sorted(by_date.items()):
        try:
            ts_day = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError:
            logger.debug(f"ARPAT bollettini range: data non parsabile: {date_str!r}")
            continue

        rec: dict[str, Any] = {
            "source": "arpat",
            "station_id": station_id,
            "location_id": location_id,
            "ts": ts_day,
            "granularity": "daily",
            "weight": weight,
            "qc_pass": True,
        }
        for arpat_var, col in _ARPAT_BOLL_VAR_MAP.items():
            if col is None:
                continue
            raw = inq_map.get(arpat_var.upper())
            try:
                rec[col] = float(raw) if raw is not None else None
            except (TypeError, ValueError):
                rec[col] = None

        records.append(rec)

    _log_scrape(
        f"arpat_bollettini_range:{location_id}:{station_id}",
        "ok",
        rows=len(by_date),
        detail=f"{start_date} to {end_date}",
    )
    return records


def fetch_arpat_bollettini_range(
    location_id: str,
    arpat_stations: list[dict[str, Any]],
    start_date: str,
    end_date: str,
) -> list[dict[str, Any]]:
    """Fetch bollettini giornalieri ARPAT su un range di date (backfill storico).

    Le stazioni vengono fetchate in parallelo con ThreadPoolExecutor(3).
    Una singola chiamata HTTP per stazione con startdate/enddate estesi.
    Restituisce tutti i record nel range — una riga wide per (stazione, giorno).

    Args:
        location_id: ID location Guazza.
        arpat_stations: lista di {"id": str, "weight": float} da locations.yaml.
        start_date, end_date: formato "YYYY-MM-DD".

    Returns:
        Lista di record wide compatibili con upsert su `observations`.
        granularity='daily', una riga per (stazione, giorno).
    """
    try:
        datetime.strptime(start_date, "%Y-%m-%d")
        datetime.strptime(end_date, "%Y-%m-%d")
    except ValueError as e:
        raise ValueError(f"start_date/end_date devono essere YYYY-MM-DD: {e}") from e

    records: list[dict[str, Any]] = []
    lock: threading.Lock = threading.Lock()

    def _fetch_and_collect(st: dict[str, Any]) -> None:
        time.sleep(0.2)
        station_records = _fetch_one_arpat_bollettini_station(
            st, location_id, start_date, end_date
        )
        if station_records:
            with lock:
                records.extend(station_records)

    with ThreadPoolExecutor(max_workers=3) as executor:
        list(tqdm(
            executor.map(_fetch_and_collect, arpat_stations),
            total=len(arpat_stations),
            desc="ARPAT bollettini range",
            unit="staz",
            disable=not sys.stderr.isatty(),
        ))

    return records


def fetch_arpat_all_locations(
    locations: dict[str, Any],
    mode: str = "nrt",
    date: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Fetch ARPAT per tutte le location che hanno arpat_stations.

    Args:
        locations: dict locations da locations.yaml["locations"].
        mode: 'nrt' (orario) o 'bollettini' (giornaliero).
        date: solo per mode='bollettini', formato YYYY-MM-DD.

    Returns:
        Dict {location_id: [record, ...]}
    """
    results: dict[str, list[dict[str, Any]]] = {}
    for loc_id, loc in locations.items():
        arpat_stations = loc.get("arpat_stations")
        if not arpat_stations:
            continue
        try:
            if mode == "nrt":
                records = fetch_arpat_nrt(loc_id, arpat_stations)
            else:
                records = fetch_arpat_bollettini(loc_id, arpat_stations, date=date)
            results[loc_id] = records
        except Exception as e:
            logger.error(f"ARPAT {mode} [{loc_id}] fallito: {e}")
            _log_scrape(f"arpat_{mode}:{loc_id}", "fail", detail=str(e))
            results[loc_id] = []
        time.sleep(0.5)
    return results


# ── CLI ───────────────────────────────────────────────────────────────────────

import typer  # noqa: E402

app = typer.Typer(help="Fetcher meteo per Guazza.")

_DB_OPTION = typer.Option(
    os.environ.get("DB_PATH", "/var/lib/guazza/guazza.duckdb"),
    "--db",
    help="Path del file DuckDB",
)


@app.command("sir-historical")
def cmd_sir_historical(
    station: str = typer.Option(..., help="ID stazione SIR"),
    sensor: str = typer.Option(..., help="Tipo sensore (termo_csv, pluvio0_24, ...)"),
    location: str = typer.Option("", help="ID location Guazza"),
) -> None:
    """Scarica storico CSV SIR e stampa le prime righe."""
    rows = fetch_sir_historical(station, sensor, location)
    typer.echo(f"Righe recuperate: {len(rows)}")
    for r in rows[:5]:
        typer.echo(str(r))


@app.command("sir-realtime")
def cmd_sir_realtime(
    station: str = typer.Option(..., help="ID stazione SIR"),
) -> None:
    """Recupera realtime SIR e stampa il record wide."""
    record = fetch_sir_realtime(station)
    typer.echo(str(record))


@app.command("netatmo")
def cmd_netatmo(
    db_path: str = _DB_OPTION,
    location: str | None = typer.Option(None, "--location", help="Solo questa location"),
) -> None:
    """Fetch Netatmo per tutte le location e salva in DuckDB."""
    from guazza.storage import DuckDBClient

    with DuckDBClient(db_path=Path(db_path)) as db:
        db.init_schema()
        results = fetch_netatmo_all_locations(db, target_location=location)

    total_stations = sum(len(v) for v in results.values())
    total_ok = sum(sum(1 for sd in v if sd.qc_pass) for v in results.values())
    typer.echo(f"\nFetch completato: {total_stations} stazioni totali, {total_ok} QC-pass")
    for loc_id, stations in results.items():
        ok = sum(1 for sd in stations if sd.qc_pass)
        typer.echo(f"  {loc_id}: {len(stations)} stazioni, {ok} QC-pass")


if __name__ == "__main__":
    app()
