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
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml
from dotenv import load_dotenv
from loguru import logger
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from guazza.weights import compute_station_weight

# ── Costanti SIR ────────────────────────────────────────────────────────────

_SIR_BASE_URL = "https://www.sir.toscana.it/archivio/download.php"
_SIR_HEADERS = {"X-Requested-With": "XMLHttpRequest"}
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


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=15))
def _fetch_sir_csv(station_id: str, sensor_type: str) -> str:
    logger.debug(f"SIR CSV fetch: {station_id} {sensor_type}")
    with httpx.Client(timeout=30) as client:
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

    logger.info(f"SIR CSV: {station_id} {sensor_type} → {len(records)} righe")
    return records


# ═════════════════════════════════════════════════════════════════════════════
# SIR — Realtime JSON
# ═════════════════════════════════════════════════════════════════════════════

_SIR_REALTIME_BASE = "https://www.sir.toscana.it/open_layers"
_SIR_RT_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.sir.toscana.it/",
}


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def fetch_sir_realtime(station_id: str) -> dict[str, Any]:
    """Recupera letture real-time per una stazione SIR.

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

    ts = datetime.now(tz=UTC)
    record: dict[str, Any] = {
        "source": "sir_toscana",
        "station_id": station_id,
        "ts": ts,
        # location_id non è noto qui; il caller lo aggiunge
    }

    # termo
    if termo := data.get("termo"):
        if v := termo.get("valore"):
            record["temp_c"] = float(v)

    # igro
    if igro := data.get("igro"):
        if v := igro.get("valore"):
            record["humidity_pct"] = float(v)

    # anemo
    if anemo := data.get("anemo"):
        if v := anemo.get("vel_media"):
            record["wind_speed_ms"] = float(v)
        if d := anemo.get("dir_media"):
            record["wind_dir_deg"] = _WIND_DIR_DEG.get(str(d).upper())
        if v := anemo.get("vel_max"):
            record["wind_gust_ms"] = float(v)

    # pluvio
    if pluvio := data.get("pluvio"):
        if (v := pluvio.get("valore")) is not None:
            record["precip_mm"] = float(v)

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
    for i, sid in enumerate(station_ids):
        if i > 0:
            time.sleep(delay)
        try:
            results[sid] = fetch_sir_realtime(sid)
        except Exception as e:
            logger.warning(f"SIR realtime fallito per {sid}: {e}")
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
            """
            SELECT temp_c FROM observations
            WHERE source = 'sir_toscana'
              AND location_id = ?
              AND ts >= (CURRENT_TIMESTAMP - INTERVAL (? || ' minutes'))
            ORDER BY ts DESC
            LIMIT 1
            """,
            [location_id, str(max_age_min)],
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


# Forward reference type workaround


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
            sd.measures.get("temp_c"),
            None,  # tmin_c
            None,  # tmax_c
            sd.measures.get("humidity_pct"),
            sd.measures.get("rain_1h"),
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
                (source, station_id, location_id, ts,
                 temp_c, tmin_c, tmax_c, humidity_pct, precip_mm,
                 wind_speed_ms, wind_dir_deg, wind_gust_ms, pressure_hpa, level_m,
                 pm10_ugm3, pm25_ugm3, no2_ugm3, o3_ugm3,
                 weight, qc_pass)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (source, station_id, ts) DO UPDATE SET
                location_id = excluded.location_id,
                temp_c        = excluded.temp_c,
                humidity_pct  = excluded.humidity_pct,
                precip_mm     = excluded.precip_mm,
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

    for loc_id, loc in locations.items():
        if target_location and loc_id != target_location:
            continue
        try:
            stations = fetch_netatmo_location(loc_id, loc, env, db=db)
            save_netatmo_to_db(db, loc_id, stations, fetched_at)
            results[loc_id] = stations
        except Exception as e:
            logger.error(f"[{loc_id}] Fetch fallito: {e}")
            results[loc_id] = []
        if not target_location:
            time.sleep(_NETATMO_DELAY)

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
