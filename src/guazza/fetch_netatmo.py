"""Fetcher Netatmo — getpublicdata real-time con QC (range, cross-validation, vs SIR).

Output: righe wide per `observations` (granularity='realtime') + log in `netatmo_fetch_log`.
"""

from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import pandas as pd
import yaml
from dotenv import load_dotenv
from loguru import logger
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential
from tqdm import tqdm

from guazza._logging import log_scrape
from guazza._paths import DEFAULT_CONFIG_DIR, REPO_ROOT
from guazza.storage import DuckDBClient
from guazza.weights import compute_station_weight

# ── Costanti Netatmo ──────────────────────────────────────────────────────────

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

_ENV_FILE = REPO_ROOT / ".env"


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
        # Scrittura atomica + permessi stretti: il .env contiene credenziali e un
        # crash a metà write_text lascerebbe il file troncato (refresh token perso).
        tmp_env = _ENV_FILE.with_suffix(".tmp")
        tmp_env.write_text(content)
        os.chmod(tmp_env, 0o600)
        tmp_env.replace(_ENV_FILE)
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
    """Timestamp della misura come datetime UTC naive (convenzione DB observations).

    L'epoch Netatmo è UTC. Restituirlo aware lo farebbe convertire in locale
    all'insert DuckDB (session TZ Europe/Rome → +2h in estate); lo strippiamo a
    naive UTC come SIR/ARPAT.
    """
    for mdata in measures.values():
        for ts_str in mdata.get("res", {}):
            try:
                return datetime.fromtimestamp(int(ts_str), tz=UTC).replace(tzinfo=None)
            except (ValueError, OSError):
                pass
    return datetime.now(tz=UTC).replace(tzinfo=None)


def _qc_range(temp: float | None, humidity: float | None) -> bool:
    if temp is not None and not (_QC_TEMP_MIN <= temp <= _QC_TEMP_MAX):
        return False
    if humidity is not None and not (0.0 <= humidity <= 100.0):
        return False
    return True


def _get_recent_sir_temp(db: DuckDBClient, location_id: str, max_age_min: int = 60) -> float | None:
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
    db: DuckDBClient | None = None,
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
    db: DuckDBClient,
    location_id: str,
    stations: list[_StationData],
    fetched_at: datetime,
) -> None:
    """Scrive in netatmo_fetch_log e observations (wide, una riga per stazione)."""
    if not stations:
        logger.warning(f"[{location_id}] Nessuna stazione da salvare")
        return

    # netatmo_fetch_log
    df_log = pd.DataFrame(
        [[fetched_at, location_id, sd.mac,
          sd.lat, sd.lon, sd.alt_m,
          sd.distance_km, sd.delta_elev_m, sd.weight,
          sd.measures.get("temp_c"), sd.measures.get("humidity_pct"),
          sd.measures.get("rain_1h"), sd.measures.get("wind_speed_ms")]
         for sd in stations],
        columns=["fetched_at", "location_id", "station_id", "lat", "lon", "alt_m",
                 "distance_km", "delta_elev_m", "weight",
                 "temperature", "humidity", "rain_1h", "wind_speed"],
    )
    db.register_df("_stg_nml", df_log)
    db.execute("""
        INSERT INTO netatmo_fetch_log
            (fetched_at, location_id, station_id, lat, lon, alt_m,
             distance_km, delta_elev_m, weight, temperature, humidity, rain_1h, wind_speed)
        SELECT fetched_at, location_id, station_id, lat, lon, alt_m,
               distance_km, delta_elev_m, weight, temperature, humidity, rain_1h, wind_speed
        FROM _stg_nml
        ON CONFLICT (fetched_at, location_id, station_id) DO NOTHING
    """)
    db.unregister_df("_stg_nml")

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
        _OBS_COLS = [
            "source", "station_id", "location_id", "ts", "granularity",
            "temp_c", "tmin_c", "tmax_c", "humidity_pct", "precip_mm", "precip_interval_h",
            "wind_speed_ms", "wind_dir_deg", "wind_gust_ms", "pressure_hpa", "level_m",
            "pm10_ugm3", "pm25_ugm3", "no2_ugm3", "o3_ugm3", "weight", "qc_pass",
        ]
        df_obs = pd.DataFrame(obs_rows, columns=_OBS_COLS)
        db.register_df("_stg_nmo", df_obs)
        db.execute("""
            INSERT INTO observations
                (source, station_id, location_id, ts, granularity,
                 temp_c, tmin_c, tmax_c, humidity_pct, precip_mm, precip_interval_h,
                 wind_speed_ms, wind_dir_deg, wind_gust_ms, pressure_hpa, level_m,
                 pm10_ugm3, pm25_ugm3, no2_ugm3, o3_ugm3,
                 weight, qc_pass)
            SELECT source, station_id, location_id, ts, granularity,
                   temp_c, tmin_c, tmax_c, humidity_pct, precip_mm, precip_interval_h,
                   wind_speed_ms, wind_dir_deg, wind_gust_ms, pressure_hpa, level_m,
                   pm10_ugm3, pm25_ugm3, no2_ugm3, o3_ugm3, weight, qc_pass
            FROM _stg_nmo
            ON CONFLICT (source, station_id, ts, granularity) DO UPDATE SET
                location_id       = excluded.location_id,
                temp_c            = excluded.temp_c,
                humidity_pct      = excluded.humidity_pct,
                precip_mm         = excluded.precip_mm,
                precip_interval_h = excluded.precip_interval_h,
                wind_speed_ms     = excluded.wind_speed_ms,
                weight            = excluded.weight,
                qc_pass           = excluded.qc_pass
        """)
        db.unregister_df("_stg_nmo")

    logger.info(
        f"[{location_id}] Salvate {len(stations)} stazioni in netatmo_fetch_log, "
        f"{len(obs_rows)} osservazioni in observations"
    )
    log_scrape(f"netatmo:{location_id}", "ok", rows=len(obs_rows))


def fetch_netatmo_all_locations(
    db: DuckDBClient,
    locations: dict[str, Any] | None = None,
    target_location: str | None = None,
) -> dict[str, list[_StationData]]:
    """Fetch Netatmo per tutte le location (o solo target_location se specificata)."""
    env = _load_env()
    if not env["access_token"] and not env["refresh_token"]:
        raise RuntimeError("NETATMO_ACCESS_TOKEN e NETATMO_REFRESH_TOKEN mancanti nel .env")

    if locations is None:
        with (DEFAULT_CONFIG_DIR / "locations.yaml").open() as f:
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
            log_scrape(f"netatmo:{loc_id}", "fail", detail=str(e))
            results[loc_id] = []
        if not target_location:
            time.sleep(_NETATMO_DELAY)

    return results


