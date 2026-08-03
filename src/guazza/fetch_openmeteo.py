"""Fetcher Open-Meteo — forecast live + Historical Forecast API (backfill, multi-lead).

Output: righe wide per la tabella `forecasts` (una per ora valida per modello NWP).
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
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
from guazza.fetch_common import UA as _UA
from guazza.fetch_common import is_retryable_http as _is_retryable_http

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
    "weather_code",
    "cape",          # Convective Available Potential Energy (J/kg)
]

# Mapping variabile Open-Meteo → colonna observations wide (solo variabili float).
# weather_code è gestito separatamente in _parse_om_response perché è int, non float.
_OM_VAR_MAP: dict[str, str] = {
    "temperature_2m": "temp_c",
    "relative_humidity_2m": "humidity_pct",
    "precipitation": "precip_mm",
    "wind_speed_10m": "wind_speed_ms",
    "wind_direction_10m": "wind_dir_deg",
    "wind_gusts_10m": "wind_gust_ms",
    "surface_pressure": "pressure_hpa",
    "cape": "cape_jkg",
}

# Cadenza run per modello (ore UTC). Usata per arrotondare ts_run.
_MODEL_RUN_HOURS: dict[str, list[int]] = {
    "ecmwf_ifs":                    [0, 6, 12, 18],
    "ecmwf_ifs025":                 [0, 6, 12, 18],
    "icon_eu":                      [0, 3, 6, 9, 12, 15, 18, 21],
    "arome_france":                 [0, 3, 6, 9, 12, 15, 18, 21],
    "italia_meteo_arpae_icon_2i":   [0, 12],
    # fallback generico
    "default": [0, 6, 12, 18],
}

# Modelli disponibili per l'area Toscana
OM_MODELS: list[str] = [
    "ecmwf_ifs",
    "icon_eu",
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

        # weather_code è un codice WMO intero — non convertire in float per evitare
        # ambiguità nel round-trip DB (INTEGER column in forecasts).
        wc_series = hourly.get("weather_code", [])
        wc_val = wc_series[i] if i < len(wc_series) else None
        rec["weather_code"] = int(wc_val) if wc_val is not None else None

        records.append(rec)

    logger.debug(
        f"Open-Meteo [{location_id}] [{model}] → {len(records)} righe"
        + (f" (ts_run={ts_run})" if ts_run is not None else " (ts_run=inferita per riga)")
    )
    return records


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
        models = OM_MODELS
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
                log_scrape(f"openmeteo_forecast:{lid}:{model}", "ok", rows=len(records))

        except Exception as e:
            logger.error(f"Open-Meteo forecast batch [{model}] fallito: {e}")
            for lid in loc_ids:
                log_scrape(f"openmeteo_forecast:{lid}:{model}", "fail", detail=str(e))

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
                log_scrape(
                    f"openmeteo_historical:{lid}:{model}",
                    "ok",
                    rows=len(records),
                    detail=f"{c_start} to {c_end}",
                )
        except Exception as e:
            logger.error(f"Open-Meteo historical batch [{model}] [{c_start}→{c_end}] fallito: {e}")
            for lid in loc_ids:
                log_scrape(f"openmeteo_historical:{lid}:{model}", "fail", detail=str(e))

        time.sleep(3.0)


# Chunk temporali per evitare timeout lato server: modelli convettivi ad alta
# risoluzione usano finestre più corte (90gg), gli altri 180gg.
_HIGH_RES_MODELS = {"arome_france", "italia_meteo_arpae_icon_2i"}
_DEFAULT_CHUNK_DAYS = 180
_HIGH_RES_CHUNK_DAYS = 90

# Worker per-modello: scrive in results[lid][model], thread-safe perché ogni
# modello ha la propria chiave nel dict annidato.
_ModelBatchWorker = Callable[
    [str, list[tuple[str, str]], list[str], list[float], list[float],
     dict[str, dict[str, list[dict[str, Any]]]]],
    None,
]


def _chunk_date_range(start_date: str, end_date: str, chunk_days: int) -> list[tuple[str, str]]:
    """Divide [start_date, end_date] in finestre contigue di chunk_days giorni (ISO)."""
    start_dt = datetime.strptime(start_date, "%Y-%m-%d").date()
    end_dt = datetime.strptime(end_date, "%Y-%m-%d").date()
    chunks: list[tuple[str, str]] = []
    curr_start = start_dt
    while curr_start <= end_dt:
        curr_end = min(curr_start + timedelta(days=chunk_days - 1), end_dt)
        chunks.append((curr_start.isoformat(), curr_end.isoformat()))
        curr_start = curr_end + timedelta(days=1)
    return chunks


def _run_historical_model_batch(
    models: list[str],
    locations: dict[str, Any],
    start_date: str,
    end_date: str,
    worker: _ModelBatchWorker,
    desc: str,
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Esegue `worker` per ogni modello in parallelo (ThreadPool 3) su chunk temporali.

    Condiviso da historical e multilead batch: stessa orchestrazione, worker diverso.
    """
    results: dict[str, dict[str, list[dict[str, Any]]]] = {
        loc_id: {model: [] for model in models} for loc_id in locations
    }
    loc_ids = sorted(locations.keys())
    lats = [locations[lid]["lat"] for lid in loc_ids]
    lons = [locations[lid]["lon"] for lid in loc_ids]
    model_chunks = {
        model: _chunk_date_range(
            start_date, end_date,
            _HIGH_RES_CHUNK_DAYS if model in _HIGH_RES_MODELS else _DEFAULT_CHUNK_DAYS,
        )
        for model in models
    }

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(worker, model, model_chunks[model], loc_ids, lats, lons, results): model
            for model in models
        }
        for future in tqdm(
            as_completed(futures),
            total=len(futures),
            desc=desc,
            unit="model",
            disable=not sys.stderr.isatty(),
        ):
            # Propaga eccezioni — se un modello fallisce, tutto fallisce
            future.result()

    return results


def fetch_openmeteo_historical_batch(
    locations: dict[str, Any],
    start_date: str,
    end_date: str,
    models: list[str] | None = None,
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Fetch storico forecast da Open-Meteo Historical Forecast API in batch.

    Returns:
        Dict {location_id: {model: [record_wide, ...]}}
    """
    if models is None:
        models = OM_MODELS
    return _run_historical_model_batch(
        models, locations, start_date, end_date,
        _fetch_one_model_historical, "OM historical batch",
    )


# ── Backfill multi-lead (D+1…D+7) via variabili *_previous_dayN ────────────────
# La Historical Forecast API restituisce di default la stima migliore (run più
# fresco per ogni ora → lead 0-5h). Le variabili `<var>_previous_dayN` espongono
# invece cosa il run di N giorni prima prevedeva per quella stessa ora valida →
# ricostruisce i lead D+1…D+7 senza deploy. L'orizzonte è model-dependent (oltre
# i giorni di forecast del modello la serie torna vuota). Verificato 2026-06-05.
_OM_PREVIOUS_DAY_MAX: dict[str, int] = {
    "ecmwf_ifs":                  7,
    "icon_eu":                    4,
    "arome_france":               1,
    "italia_meteo_arpae_icon_2i": 2,
}

# Solo le variabili usate dalla feature pipeline (features.py daily_nwp). Le altre
# (pressione, raffica, weather_code) non entrano nel modello → non servono al backtest.
_OM_MULTILEAD_VARS: dict[str, str] = {
    "temperature_2m":       "temp_c",
    "precipitation":        "precip_mm",
    "relative_humidity_2m": "humidity_pct",
    "wind_speed_10m":       "wind_speed_ms",
}


def _multilead_hourly_params(model: str) -> list[str]:
    """Variabili `<var>_previous_dayN` per i lead archiviati dal modello."""
    max_n = _OM_PREVIOUS_DAY_MAX.get(model, 0)
    return [f"{var}_previous_day{n}"
            for n in range(1, max_n + 1)
            for var in _OM_MULTILEAD_VARS]


def _parse_om_multilead(
    data: dict[str, Any],
    model: str,
    location_id: str,
) -> list[dict[str, Any]]:
    """Espande le serie `<var>_previous_dayN` in record forecasts multi-lead.

    Per ogni ora valida T e ogni N disponibile: ts_valid=T, lead_time_h=24N,
    ts_run = mezzanotte UTC del giorno (T − N giorni). Così features.daily_nwp
    calcola DATEDIFF=N → lead_time_days=N e aggrega l'intera giornata a un daily
    a lead 24N. Le righe con tutte le variabili null vengono saltate.
    """
    hourly = data.get("hourly", {})
    times: list[str] = hourly.get("time", [])
    if not times:
        return []

    max_n = _OM_PREVIOUS_DAY_MAX.get(model, 0)
    records: list[dict[str, Any]] = []
    for i, time_str in enumerate(times):
        try:
            ts_valid = datetime.fromisoformat(time_str).replace(tzinfo=UTC)
        except ValueError:
            continue
        for n in range(1, max_n + 1):
            vals: dict[str, float | None] = {}
            for om_var, col in _OM_MULTILEAD_VARS.items():
                series = hourly.get(f"{om_var}_previous_day{n}", [])
                v = series[i] if i < len(series) else None
                vals[col] = float(v) if v is not None else None
            if all(v is None for v in vals.values()):
                continue
            ts_run = datetime(
                ts_valid.year, ts_valid.month, ts_valid.day, tzinfo=UTC
            ) - timedelta(days=n)
            records.append({
                "source": f"open_meteo_{model}",
                "location_id": location_id,
                "ts_run": ts_run,
                "ts_valid": ts_valid,
                "lead_time_h": 24 * n,
                **vals,
            })
    return records


def _fetch_one_model_multilead(
    model: str,
    chunks: list[tuple[str, str]],
    loc_ids: list[str],
    lats: list[float],
    lons: list[float],
    results: dict[str, dict[str, list[dict[str, Any]]]],
) -> None:
    """Fetch multi-lead per un singolo modello su tutti i chunk."""
    hourly_vars = _multilead_hourly_params(model)
    if not hourly_vars:
        return  # modello senza run archiviati
    for c_start, c_end in chunks:
        params: dict[str, str | int | float | list[str]] = {
            "latitude": ",".join(map(str, lats)),
            "longitude": ",".join(map(str, lons)),
            "hourly": ",".join(hourly_vars),
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
                records = _parse_om_multilead(resp, model, lid)
                results[lid][model].extend(records)
                log_scrape(
                    f"openmeteo_multilead:{lid}:{model}",
                    "ok",
                    rows=len(records),
                    detail=f"{c_start} to {c_end}",
                )
        except Exception as e:
            logger.error(f"Open-Meteo multilead [{model}] [{c_start}→{c_end}] fallito: {e}")
            for lid in loc_ids:
                log_scrape(f"openmeteo_multilead:{lid}:{model}", "fail", detail=str(e))
        time.sleep(3.0)


def fetch_openmeteo_multilead_batch(
    locations: dict[str, Any],
    start_date: str,
    end_date: str,
    models: list[str] | None = None,
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Backfill multi-lead D+1…D+7 da Open-Meteo (variabili `*_previous_dayN`).

    Stessa struttura di fetch_openmeteo_historical_batch (chunk + ThreadPool(3)),
    ma richiede i run precedenti. Salta i modelli senza orizzonte archiviato.

    Returns:
        Dict {location_id: {model: [record_wide, ...]}}
    """
    if models is None:
        models = [m for m in OM_MODELS if _OM_PREVIOUS_DAY_MAX.get(m, 0) > 0]
    return _run_historical_model_batch(
        models, locations, start_date, end_date,
        _fetch_one_model_multilead, "OM multilead batch",
    )


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


