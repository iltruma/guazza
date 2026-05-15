"""Fetcher Netatmo real-time via getpublicdata (fetch dinamico, no lista MAC fissa).

Per ogni location:
  1. Chiama getpublicdata con bbox intorno a lat/lon della location
  2. Per ogni stazione nell'area: calcola peso decay (distanza + delta quota)
  3. QC inline:
     - Range check: temperature ∈ [-20, 50]°C, humidity ∈ [0, 100]%
     - Cross-validation: |T_stazione - T_media_pesata| <= 5°C
  4. Salva in netatmo_fetch_log (una riga per stazione per fetch)
  5. Salva in observations (una riga per variabile per stazione, con peso e qc_pass)

Token management:
  - Legge NETATMO_ACCESS_TOKEN da env / .env
  - Auto-refresh via NETATMO_REFRESH_TOKEN + CLIENT_ID/SECRET su 401/403
  - Aggiorna .env dopo il refresh

CLI:
    uv run python -m guazza.ingestion.netatmo_realtime fetch
    uv run python -m guazza.ingestion.netatmo_realtime fetch --location casa_campi
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml
from dotenv import load_dotenv
from loguru import logger
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from guazza.storage.station_weights import compute_station_weight

# ── Costanti ──────────────────────────────────────────────────────────────────

_REPO_ROOT      = Path(__file__).resolve().parents[3]
_ENV_FILE       = _REPO_ROOT / ".env"
_CONFIG_DIR     = Path(os.environ.get("CONFIG_DIR", str(_REPO_ROOT / "config")))

GETPUBLICDATA_URL = "https://api.netatmo.com/api/getpublicdata"
TOKEN_URL         = "https://api.netatmo.com/oauth2/token"

BBOX_PAD_DEG   = 0.06    # ~6.6 km in ogni direzione
QC_TEMP_MIN    = -20.0   # °C
QC_TEMP_MAX    =  50.0   # °C
QC_CROSS_SIGMA =   5.0   # °C — soglia cross-validation rispetto alla mediana Netatmo peers
QC_SIR_SIGMA   =   8.0   # °C — soglia vs stazione SIR validata (margine più ampio per distanza)
INTER_LOC_DELAY = 1.0    # secondi tra chiamate API (rate limit cortesia)

# Variabili Netatmo → nome interno observations
_VAR_MAP = {
    "temperature":  "temperature_2m",
    "humidity":     "humidity",
    "sum_rain_1":   "rain_1h",
    "rain_60min":   "rain_1h",
    "wind_strength": "wind_speed",
}


# ── Token management ──────────────────────────────────────────────────────────


def _load_env() -> dict[str, str]:
    load_dotenv(_ENV_FILE)
    return {
        "access_token":  os.getenv("NETATMO_ACCESS_TOKEN", ""),
        "refresh_token": os.getenv("NETATMO_REFRESH_TOKEN", ""),
        "client_id":     os.getenv("NETATMO_CLIENT_ID", ""),
        "client_secret": os.getenv("NETATMO_CLIENT_SECRET", ""),
    }


def _refresh_token(env: dict[str, str]) -> str:
    """Rinnova l'access token via refresh_token. Aggiorna .env e restituisce il nuovo token."""
    if not env["refresh_token"] or not env["client_id"]:
        raise RuntimeError("NETATMO_REFRESH_TOKEN o NETATMO_CLIENT_ID mancanti — impossibile rinnovare")
    logger.info("Rinnovo access token Netatmo via refresh_token…")
    resp = httpx.post(
        TOKEN_URL,
        data={
            "grant_type":    "refresh_token",
            "client_id":     env["client_id"],
            "client_secret": env["client_secret"],
            "refresh_token": env["refresh_token"],
        },
        timeout=15,
    )
    resp.raise_for_status()
    tokens = resp.json()
    new_access  = tokens["access_token"]
    new_refresh = tokens.get("refresh_token", env["refresh_token"])

    # Aggiorna .env in-place
    if _ENV_FILE.exists():
        content = _ENV_FILE.read_text()
        content = re.sub(r"NETATMO_ACCESS_TOKEN=\S*",  f"NETATMO_ACCESS_TOKEN={new_access}",  content)
        content = re.sub(r"NETATMO_REFRESH_TOKEN=\S*", f"NETATMO_REFRESH_TOKEN={new_refresh}", content)
        _ENV_FILE.write_text(content)
    logger.info("Token rinnovato e salvato nel .env")
    return new_access


# ── Chiamata API ──────────────────────────────────────────────────────────────


def _is_retryable(exc: BaseException) -> bool:
    """Non ritentare su 401/403: gestiti da _call_with_refresh con token refresh."""
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (401, 403):
        return False
    return True


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception(_is_retryable),
)
def _fetch_public_data(token: str, lat: float, lon: float) -> list[dict[str, Any]]:
    """getpublicdata con bbox intorno a (lat, lon). Non gestisce 401/403 (delega al caller)."""
    resp = httpx.get(
        GETPUBLICDATA_URL,
        params={
            "lat_ne": lat + BBOX_PAD_DEG,
            "lon_ne": lon + BBOX_PAD_DEG,
            "lat_sw": lat - BBOX_PAD_DEG,
            "lon_sw": lon - BBOX_PAD_DEG,
            "filter": "true",
        },
        headers={"Authorization": f"Bearer {token}"},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "ok":
        raise ValueError(f"API error: {data}")
    return data.get("body", [])


def _call_with_refresh(env: dict[str, str], lat: float, lon: float) -> list[dict[str, Any]]:
    """Chiama getpublicdata; se 401/403 rinnova il token e riprova una volta."""
    token = env["access_token"]
    try:
        return _fetch_public_data(token, lat, lon)
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (401, 403):
            token = _refresh_token(env)
            env["access_token"] = token
            return _fetch_public_data(token, lat, lon)
        raise


# ── Estrazione misure ─────────────────────────────────────────────────────────


def _extract_measures(measures: dict[str, Any]) -> dict[str, float | None]:
    """Estrae temperatura, umidità, pioggia, vento dal dict measures di getpublicdata.

    Il dict measures è keyed per MAC del modulo. Ogni entry ha:
      - 'type': lista nomi variabili  (es. ['temperature', 'humidity'])
      - 'res':  {timestamp_str: [values...]}  — una sola entry (lettura più recente)

    Restituisce None per variabili non disponibili per questa stazione.
    """
    result: dict[str, float | None] = {
        "temperature_2m": None,
        "humidity":       None,
        "rain_1h":        None,
        "wind_speed":     None,
    }

    for _mac, mdata in measures.items():
        types: list[str] = mdata.get("type", [])
        res: dict[str, list[float]] = mdata.get("res", {})
        if not res:
            continue
        values = next(iter(res.values()))  # unica entry = lettura più recente
        for i, vtype in enumerate(types):
            if i >= len(values):
                continue
            internal = _VAR_MAP.get(vtype)
            if internal and result[internal] is None:
                result[internal] = float(values[i])

    return result


def _measure_ts(measures: dict[str, Any]) -> datetime:
    """Ricava il timestamp della misura dal dict measures (primo modulo con dati)."""
    for mdata in measures.values():
        for ts_str in mdata.get("res", {}):
            try:
                return datetime.fromtimestamp(int(ts_str), tz=timezone.utc)
            except (ValueError, OSError):
                pass
    return datetime.now(tz=timezone.utc)


# ── QC ────────────────────────────────────────────────────────────────────────


def _qc_range(temp: float | None, humidity: float | None) -> bool:
    """Range check: temperatura ∈ [-20, 50]°C, umidità ∈ [0, 100]%."""
    if temp is not None and not (QC_TEMP_MIN <= temp <= QC_TEMP_MAX):
        return False
    if humidity is not None and not (0.0 <= humidity <= 100.0):
        return False
    return True


def _weighted_mean(values_weights: list[tuple[float, float]]) -> float | None:
    """Media pesata. Restituisce None se la lista è vuota."""
    total_w = sum(w for _, w in values_weights)
    if total_w == 0:
        return None
    return sum(v * w for v, w in values_weights) / total_w


def _get_recent_sir_temp(db: Any, location_id: str, max_age_min: int = 60) -> float | None:
    """Legge l'ultima temperatura SIR da observations (entro max_age_min minuti).

    Restituisce None se non ci sono dati recenti — QC SIR viene saltato silenziosamente.
    """
    try:
        row = db.execute(
            """
            SELECT value FROM observations
            WHERE source = 'sir'
              AND location_id = ?
              AND variable = 'temperature_2m'
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


def _apply_sir_qc(
    stations: list[Any],  # list[_StationData] — forward ref
    sir_temp: float,
    threshold_c: float = QC_SIR_SIGMA,
) -> None:
    """Applica il QC secondario vs temperatura SIR: imposta qc_sir=False se |T - T_sir| > threshold_c."""
    for sd in stations:
        t = sd.measures["temperature_2m"]
        if t is not None:
            sd.qc_sir = abs(t - sir_temp) <= threshold_c


# ── Struttura dati intermedia ─────────────────────────────────────────────────


from dataclasses import dataclass, field  # noqa: E402  (after constants)


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
    qc_sir:   bool = True

    @property
    def qc_pass(self) -> bool:
        return self.qc_range and self.qc_cross and self.qc_sir


# ── Core fetch per singola location ──────────────────────────────────────────


def fetch_location(
    location_id: str,
    loc: dict[str, Any],
    env: dict[str, str],
    db: Any | None = None,
) -> list[_StationData]:
    """Chiama getpublicdata per una location e restituisce le stazioni processate.

    QC applicato in sequenza:
      1. Range check (temperatura ∈ [-20, 50]°C, umidità ∈ [0, 100]%)
      2. Cross-validation vs mediana Netatmo peers (±5°C)
      3. Confronto vs SIR validata (±8°C) — solo se db fornito e dati recenti disponibili
    """
    target_lat:  float = loc["lat"]
    target_lon:  float = loc["lon"]
    target_elev: float = loc["elevation_m"]

    raw_stations = _call_with_refresh(env, target_lat, target_lon)
    logger.info(f"[{location_id}] getpublicdata → {len(raw_stations)} stazioni nell'area")

    results: list[_StationData] = []
    for s in raw_stations:
        place    = s.get("place", {})
        location = place.get("location", [None, None])   # [lon, lat] — lon prima!
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
            qc_range=_qc_range(m["temperature_2m"], m["humidity"]),
        )
        results.append(sd)

    # Cross-validation sulla temperatura — usa la mediana come riferimento
    # (robusta agli outlier: un singolo valore anomalo non inquina il riferimento)
    valid_temps_list = sorted(
        sd.measures["temperature_2m"]
        for sd in results
        if sd.qc_range and sd.measures["temperature_2m"] is not None
    )
    if valid_temps_list:
        mid = len(valid_temps_list) // 2
        t_ref = (
            valid_temps_list[mid]
            if len(valid_temps_list) % 2 == 1
            else (valid_temps_list[mid - 1] + valid_temps_list[mid]) / 2
        )
        for sd in results:
            t = sd.measures["temperature_2m"]
            if t is not None:
                sd.qc_cross = abs(t - t_ref) <= QC_CROSS_SIGMA

    # QC terziario: confronto vs stazione SIR validata (se disponibile)
    if db is not None:
        sir_temp = _get_recent_sir_temp(db, location_id)
        if sir_temp is not None:
            _apply_sir_qc(results, sir_temp)
            n_sir_fail = sum(1 for sd in results if not sd.qc_sir)
            logger.info(
                f"[{location_id}] QC SIR (T_ref={sir_temp:.1f}°C): "
                f"{n_sir_fail} stazioni escluse"
            )
        else:
            logger.debug(f"[{location_id}] QC SIR saltato: nessuna osservazione SIR recente")

    n_ok  = sum(1 for sd in results if sd.qc_pass)
    n_fail = len(results) - n_ok
    logger.info(f"[{location_id}] QC finale: {n_ok} OK, {n_fail} escluse")
    return results


# ── Persistenza ───────────────────────────────────────────────────────────────


def save_to_db(
    db: Any,   # DuckDBClient — type annotation evita import circolare
    location_id: str,
    stations: list[_StationData],
    fetched_at: datetime,
) -> None:
    """Scrive in netatmo_fetch_log e observations."""
    if not stations:
        logger.warning(f"[{location_id}] Nessuna stazione da salvare")
        return

    # ── netatmo_fetch_log ─────────────────────────────────────────────────────
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
                sd.measures["temperature_2m"],
                sd.measures["humidity"],
                sd.measures["rain_1h"],
                sd.measures["wind_speed"],
            ]
            for sd in stations
        ],
    )

    # ── observations — una riga per (stazione, ts, variabile) ─────────────────
    obs_rows: list[list[Any]] = []
    for sd in stations:
        for internal_var, value in sd.measures.items():
            if value is None:
                continue
            obs_rows.append([
                "netatmo", sd.mac, location_id,
                sd.ts, internal_var, value,
                "ok", sd.weight, sd.qc_pass,
            ])

    if obs_rows:
        db.executemany(
            """
            INSERT INTO observations
                (source, station_id, location_id, ts, variable, value, flag, weight, qc_pass)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (source, station_id, location_id, ts, variable) DO UPDATE SET
                value    = excluded.value,
                weight   = excluded.weight,
                qc_pass  = excluded.qc_pass
            """,
            obs_rows,
        )

    logger.info(
        f"[{location_id}] Salvate {len(stations)} stazioni in netatmo_fetch_log, "
        f"{len(obs_rows)} osservazioni in observations"
    )


# ── Funzione principale ───────────────────────────────────────────────────────


def fetch_all_locations(
    db: Any,
    locations: dict[str, Any] | None = None,
    target_location: str | None = None,
) -> dict[str, list[_StationData]]:
    """Fetch Netatmo per tutte le location (o solo target_location se specificata).

    Returns:
        Dict location_id → lista stazioni processate.
    """
    env = _load_env()
    if not env["access_token"] and not env["refresh_token"]:
        raise RuntimeError(
            "NETATMO_ACCESS_TOKEN e NETATMO_REFRESH_TOKEN mancanti nel .env"
        )

    if locations is None:
        with (_CONFIG_DIR / "locations.yaml").open() as f:
            locations = yaml.safe_load(f)["locations"]

    fetched_at = datetime.now(tz=timezone.utc)
    results: dict[str, list[_StationData]] = {}

    for loc_id, loc in locations.items():
        if target_location and loc_id != target_location:
            continue

        try:
            stations = fetch_location(loc_id, loc, env, db=db)
            save_to_db(db, loc_id, stations, fetched_at)
            results[loc_id] = stations
        except Exception as e:
            logger.error(f"[{loc_id}] Fetch fallito: {e}")
            results[loc_id] = []

        if not target_location:
            time.sleep(INTER_LOC_DELAY)

    return results


# ── CLI ───────────────────────────────────────────────────────────────────────

import typer  # noqa: E402

app = typer.Typer(help="Fetch Netatmo real-time via getpublicdata.")

_DB_OPTION = typer.Option(
    os.environ.get("DB_PATH", "/var/lib/guazza/guazza.duckdb"),
    "--db", help="Path del file DuckDB",
)


@app.command("fetch")
def cmd_fetch(
    db_path: str = _DB_OPTION,
    location: str | None = typer.Option(None, "--location", help="Solo questa location"),
) -> None:
    """Fetch Netatmo per tutte le location e salva in DuckDB."""
    from guazza.storage.duckdb_client import DuckDBClient

    with DuckDBClient(db_path=Path(db_path)) as db:
        db.init_schema()
        db.run_migrations()
        results = fetch_all_locations(db, target_location=location)

    total_stations = sum(len(v) for v in results.values())
    total_ok = sum(
        sum(1 for sd in v if sd.qc_pass)
        for v in results.values()
    )
    typer.echo(f"\nFetch completato: {total_stations} stazioni totali, {total_ok} QC-pass")
    for loc_id, stations in results.items():
        ok = sum(1 for sd in stations if sd.qc_pass)
        typer.echo(f"  {loc_id}: {len(stations)} stazioni, {ok} QC-pass")


if __name__ == "__main__":
    app()
