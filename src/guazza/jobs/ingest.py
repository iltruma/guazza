"""Entry point cron — ingestion dati Guazza.

Quattro comandi separati con schedulazioni diverse:

  historical  — one-shot: backfill completo SIR CSV + Open-Meteo historical (2022→oggi)
  daily       — cron 1×/giorno: delta di ieri (SIR CSV + Open-Meteo historical)
  realtime    — cron ogni 15-30 min: SIR actions.php + Netatmo
  forecasts   — cron ogni 6h: Open-Meteo forecast (7 giorni, tutti i modelli)

Uso:
    uv run python -m guazza.jobs.ingest historical [--start-date 2022-01-01]
    uv run python -m guazza.jobs.ingest daily
    uv run python -m guazza.jobs.ingest realtime
    uv run python -m guazza.jobs.ingest forecasts

Variabili d'ambiente:
    DB_PATH           — path file DuckDB (default: /var/lib/guazza/guazza.duckdb)
    CONFIG_DIR        — directory YAML config (default: <repo>/config)
    HEALTHCHECKS_URL  — URL ping Healthchecks.io (opzionale; se assente, ping saltato)
"""

from __future__ import annotations

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import typer
import yaml
from dotenv import load_dotenv
from loguru import logger
from tqdm import tqdm

# Carica .env prima di leggere variabili d'ambiente (es. DB_PATH)
_REPO_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(_REPO_ROOT / ".env")

from guazza._logging import setup_logging  # noqa: E402
from guazza.fetchers import (  # noqa: E402
    _OM_MODELS,
    _log_scrape,
    fetch_netatmo_all_locations,
    fetch_openaq_all_locations,
    fetch_openaq_measurements_range,
    fetch_openmeteo_all_locations,
    fetch_openmeteo_historical_batch,
    fetch_sir_bulk_realtime,
    fetch_sir_historical,
    fetch_sir_stations_realtime,
)
from guazza.storage import DuckDBClient  # noqa: E402

# ── Costanti ─────────────────────────────────────────────────────────────────

_DEFAULT_DB = Path(os.environ.get("DB_PATH", "/var/lib/guazza/guazza.duckdb"))
_DEFAULT_CFG = Path(os.environ.get("CONFIG_DIR", str(_REPO_ROOT / "config")))

# Mappatura sensore fisico (stations.yaml) → IDST endpoint CSV SIR
_SENSOR_TO_IDST: dict[str, str] = {
    "termometro": "termo_csv",
    "pluviometro": "pluvio0_24",
    "igrometro": "igro0_24",
    "anemometro": "anemo0_24",
    "idrometro": "idro_l",
}

# Sensori solo realtime — nessun CSV storico disponibile
_REALTIME_ONLY_SENSORS = {"barometro", "radiometro_diretta", "radiometro_UV",
                           "radiometro_solare", "evaporimetro"}

# Throttle tra fetch SIR CSV (rispettare ~1.2s/req)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_config(cfg_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Carica locations.yaml e stations.yaml. Restituisce (locations, stations)."""
    with (cfg_dir / "locations.yaml").open() as f:
        locations: dict[str, Any] = yaml.safe_load(f)["locations"]
    with (cfg_dir / "stations.yaml").open() as f:
        stations: dict[str, Any] = yaml.safe_load(f)
    return locations, stations


def _all_sir_station_ids(locations: dict[str, Any]) -> set[str]:
    """Raccoglie tutti gli ID stazione SIR usati (meteo + idro + upstream) su tutte le location."""
    ids: set[str] = set()
    for loc in locations.values():
        for sensor_list in loc.get("sir_stations", {}).values():
            ids.update(sensor_list)
        if idro_id := loc.get("sir_idro_id"):
            ids.add(idro_id)
        ids.update(loc.get("upstream_pluvio_stations", []))
    return ids


def _idst_for_station(station_id: str, stations: dict[str, Any]) -> list[str]:
    """Restituisce la lista di IDST scaricabili per una stazione (esclude realtime-only)."""
    s = stations.get("sir_stations", {}).get(station_id, {})
    idst_list: list[str] = []
    for sensor in s.get("sensors", []):
        if sensor in _REALTIME_ONLY_SENSORS:
            continue
        idst = _SENSOR_TO_IDST.get(sensor)
        if idst:
            idst_list.append(idst)
    return idst_list


def _ping_healthchecks(status: str = "") -> None:
    """Invia ping a Healthchecks.io se HEALTHCHECKS_URL è configurato.

    status: "" = ok, "/fail" = fail, "/start" = start.
    """
    base_url = os.environ.get("HEALTHCHECKS_URL", "").strip()
    if not base_url:
        return
    url = base_url.rstrip("/") + status
    try:
        httpx.get(url, timeout=5)
        logger.debug(f"Healthchecks ping: {url}")
    except Exception as e:
        logger.warning(f"Healthchecks ping fallito: {e}")


def _idro_station_ids(locations: dict[str, Any], stations: dict[str, Any]) -> set[str]:
    """Restituisce gli ID delle stazioni idrometriche.

    Include sia i sir_idro_id espliciti nelle location sia le stazioni con
    sensore 'idrometro' in stations.yaml. Per queste stazioni il livello
    idrometrico non è disponibile negli endpoint bulk (IDRO restituisce
    livelli di allerta, non valori reali) — serve la chiamata per-stazione.
    """
    ids: set[str] = set()
    for loc in locations.values():
        if idro_id := loc.get("sir_idro_id"):
            ids.add(idro_id)
    for sid, s_data in stations.get("sir_stations", {}).items():
        if "idrometro" in s_data.get("sensors", []):
            ids.add(sid)
    return ids


def _location_id_for_station(
    station_id: str,
    locations: dict[str, Any],
) -> str:
    """Restituisce il primo location_id che usa questa stazione, o stringa vuota.

    Le stazioni upstream_pluvio_stations vengono associate alla prima location
    che le elenca — il location_id è necessario per il NOT NULL di observations,
    ma il ring CTE in features.py fa JOIN su station_id, non location_id.
    """
    for loc_id, loc in locations.items():
        for sensor_list in loc.get("sir_stations", {}).values():
            if station_id in sensor_list:
                return loc_id
        if loc.get("sir_idro_id") == station_id:
            return loc_id
    for loc_id, loc in locations.items():
        if station_id in loc.get("upstream_pluvio_stations", []):
            return loc_id
    return ""


# ── Ingestion SIR storico ─────────────────────────────────────────────────────

def _ingest_sir_historical_range(
    db: DuckDBClient,
    locations: dict[str, Any],
    stations: dict[str, Any],
    start_date: str,
    end_date: str,
) -> int:
    """Scarica SIR CSV per tutte le stazioni e tutti i sensori nell'intervallo.

    Le stazioni+sensori vengono fetchati in parallelo con ThreadPoolExecutor(3),
    poi i record filtrati vengono scritti in DB sequenzialmente.

    Per il backfill completo l'API SIR restituisce sempre tutto lo storico
    disponibile — i parametri start/end_date sono usati solo per filtrare
    i record dopo il parsing (il CSV non supporta range).

    Returns: numero totale di record inseriti.
    """
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")

    station_ids = _all_sir_station_ids(locations)

    # Costruisce lista di (sid, idst, loc_id) per tutte le combinazioni
    combos: list[tuple[str, str, str]] = []
    for sid in station_ids:
        loc_id = _location_id_for_station(sid, locations)
        idst_list = _idst_for_station(sid, stations)
        for idst in idst_list:
            combos.append((sid, idst, loc_id))

    if not combos:
        return 0

    def _fetch_one(sid: str, idst: str, loc_id: str) -> tuple[str, str, list[dict[str, Any]]] | None:
        """Fetch e filtra un singolo combo stazione+sensore."""
        try:
            records = fetch_sir_historical(sid, idst, loc_id)
            filtered = [r for r in records if start_dt <= r["ts"] <= end_dt]
            if filtered:
                _log_scrape(f"sir_historical:{sid}:{idst}", "ok", rows=len(filtered))
                return (sid, idst, filtered)
            return None
        except httpx.HTTPStatusError as e:
            logger.opt(exception=True).error(
                f"SIR historical [{sid}] {idst}: HTTP {e.response.status_code} "
                f"su {e.request.url}"
            )
            _log_scrape(f"sir_historical:{sid}:{idst}", "fail", detail=f"HTTP {e.response.status_code}")
            return None
        except Exception as e:
            logger.opt(exception=True).error(f"SIR historical [{sid}] {idst} fallito: {e}")
            _log_scrape(f"sir_historical:{sid}:{idst}", "fail", detail=str(e))
            return None

    results: list[tuple[str, str, list[dict[str, Any]]]] = []
    _tty = sys.stderr.isatty()
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(_fetch_one, sid, idst, loc_id): (sid, idst)
            for sid, idst, loc_id in combos
        }
        for future in tqdm(
            as_completed(futures),
            total=len(futures),
            desc="SIR historical",
            unit="combo",
            disable=not _tty,
        ):
            result = future.result()
            if result:
                results.append(result)

    total = 0
    for sid, idst, filtered in results:
        db.upsert_sir_observations(filtered)
        total += len(filtered)
        logger.info(f"[{sid}] {idst}: {len(filtered)} righe ({start_date}→{end_date})")

    return total


# ── CLI ───────────────────────────────────────────────────────────────────────

app = typer.Typer(
    help="Ingestion dati Guazza — SIR, Netatmo, Open-Meteo.",
    no_args_is_help=True,
)

_DB_OPT = typer.Option(_DEFAULT_DB, "--db", help="Path file DuckDB")
_CFG_OPT = typer.Option(str(_DEFAULT_CFG), "--config-dir", help="Directory YAML config")


def _setup_logging(level: str = "INFO") -> None:
    setup_logging(level)


@app.command("historical")
def cmd_historical(
    db_path: Path = _DB_OPT,
    config_dir: str = _CFG_OPT,
    start_date: str = typer.Option("2022-01-01", "--start-date", help="Inizio intervallo YYYY-MM-DD"),
    end_date: str = typer.Option("", "--end-date", help="Fine intervallo YYYY-MM-DD (default: oggi)"),
    only_sir: bool = typer.Option(False, "--only-sir", help="Scarica solo SIR CSV, salta Open-Meteo e OpenAQ"),
    only_openmeteo: bool = typer.Option(False, "--only-openmeteo", help="Scarica solo Open-Meteo, salta SIR e OpenAQ"),
    only_openaq: bool = typer.Option(False, "--only-openaq", help="Scarica solo OpenAQ storico, salta SIR e Open-Meteo"),
    location: list[str] | None = typer.Option(None, "--location", help="Limita a questa location (ripetibile)"),
    om_model: list[str] | None = typer.Option(None, "--om-model", help="Limita Open-Meteo a questo modello (ripetibile). Es: --om-model italia_meteo_arpae_icon_2i"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Stampa cosa farebbe senza scrivere"),
) -> None:
    """Backfill completo: SIR CSV + Open-Meteo historical + OpenAQ storico (one-shot, lento).

    Da eseguire una volta sola per caricare lo storico di training.
    Non schedulare come cron — usa 'daily' per il delta incrementale.

    Esempi:
        # Solo Open-Meteo per una location
        historical --only-openmeteo --location casa_campi

        # Solo SIR, intervallo ridotto
        historical --only-sir --start-date 2024-01-01

        # Solo OpenAQ qualità aria storica
        historical --only-openaq --start-date 2022-01-01

        # Tutte le sorgenti, tutte le location
        historical --start-date 2022-01-01
    """
    _setup_logging()
    exclusive = sum([only_sir, only_openmeteo, only_openaq])
    if exclusive > 1:
        typer.echo("Errore: --only-sir, --only-openmeteo, --only-openaq sono mutualmente esclusivi.")
        raise typer.Exit(1)
    if om_model:
        unknown_models = set(om_model) - set(_OM_MODELS)
        if unknown_models:
            typer.echo(f"Errore: modelli sconosciuti: {sorted(unknown_models)}")
            typer.echo(f"Disponibili: {_OM_MODELS}")
            raise typer.Exit(1)
    if not end_date:
        end_date = datetime.now(tz=UTC).strftime("%Y-%m-%d")

    cfg = Path(config_dir)
    locations_all, stations = _load_config(cfg)

    # Filtra location se specificate
    if location:
        unknown = set(location) - set(locations_all)
        if unknown:
            typer.echo(f"Errore: location sconosciute: {sorted(unknown)}")
            typer.echo(f"Disponibili: {list(locations_all.keys())}")
            raise typer.Exit(1)
        locations = {k: v for k, v in locations_all.items() if k in location}
    else:
        locations = locations_all

    run_sir = not only_openmeteo and not only_openaq
    run_om = not only_sir and not only_openaq
    run_openaq = not only_sir and not only_openmeteo

    typer.echo(f"Historical backfill: {start_date} → {end_date}")
    typer.echo(f"Location: {list(locations.keys())}")
    sorgenti = " ".join(filter(None, [
        "SIR" if run_sir else "",
        "Open-Meteo" if run_om else "",
        "OpenAQ" if run_openaq else "",
    ]))
    typer.echo(f"Sorgenti: {sorgenti}")
    if run_sir:
        typer.echo(f"Stazioni SIR: {len(_all_sir_station_ids(locations))}")

    if dry_run:
        typer.echo("[dry-run] Nessuna scrittura effettuata.")
        return

    _ping_healthchecks("/start")
    t0 = time.monotonic()
    ok = True
    sir_total = 0
    om_total = 0
    aq_total = 0

    try:
        with DuckDBClient(db_path=db_path) as db:
            db.init_schema()

            if run_sir:
                typer.echo("\n--- SIR storico CSV ---")
                sir_total = _ingest_sir_historical_range(
                    db, locations, stations, start_date, end_date
                )
                typer.echo(f"SIR CSV: {sir_total} record inseriti")

            if run_om:
                typer.echo("\n--- Open-Meteo historical (batch) ---")
                # L'archivio Historical Forecast API arriva fino a 2 giorni fa
                om_end_date = min(
                    datetime.fromisoformat(end_date).date(),
                    (datetime.now(tz=UTC) - timedelta(days=2)).date(),
                ).isoformat()
                if om_end_date != end_date:
                    typer.echo(f"Open-Meteo: end_date cappato a {om_end_date} (archivio arriva a oggi-2gg)")
                results_all = fetch_openmeteo_historical_batch(
                    locations=locations,
                    start_date=start_date,
                    end_date=om_end_date,
                    models=om_model or None,
                )
                for _loc_id, model_results in results_all.items():
                    for _model, records in model_results.items():
                        if records:
                            om_total += db.upsert_forecasts(records)
                typer.echo(f"Open-Meteo historical: {om_total} record inseriti")

            if run_openaq:
                typer.echo("\n--- OpenAQ storico ---")
                for loc_id, loc in locations.items():
                    if "aria_qualita" not in (loc.get("extras") or []):
                        continue
                    loc_records = fetch_openaq_measurements_range(
                        location_id=loc_id,
                        lat=loc["lat"],
                        lon=loc["lon"],
                        start_date=start_date,
                        end_date=end_date,
                    )
                    if loc_records:
                        aq_total += db.upsert_sir_observations(loc_records)
                typer.echo(f"OpenAQ storico: {aq_total} record inseriti")

    except Exception as e:
        logger.error(f"historical fallito: {e}")
        _ping_healthchecks("/fail")
        ok = False
        raise typer.Exit(1) from e

    elapsed = time.monotonic() - t0
    _log_scrape("job_historical", "ok" if ok else "fail",
                rows=sir_total + om_total + aq_total if ok else None)
    _ping_healthchecks()
    typer.echo(f"\nCompletato in {elapsed:.0f}s — SIR:{sir_total} OM:{om_total} OpenAQ:{aq_total}")


@app.command("daily")
def cmd_daily(
    db_path: Path = _DB_OPT,
    config_dir: str = _CFG_OPT,
    date: str = typer.Option("", "--date", help="Giorno da caricare YYYY-MM-DD (default: ieri)"),
    only_sir: bool = typer.Option(False, "--only-sir", help="Scarica solo SIR CSV, salta Open-Meteo"),
    only_openmeteo: bool = typer.Option(False, "--only-openmeteo", help="Scarica solo Open-Meteo, salta SIR"),
    location: list[str] | None = typer.Option(None, "--location", help="Limita a questa location (ripetibile)"),
    om_model: list[str] | None = typer.Option(None, "--om-model", help="Limita Open-Meteo a questo modello (ripetibile)"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Delta incrementale giornaliero: SIR CSV + Open-Meteo per il giorno indicato.

    Schedulare a ~06:00 UTC (SIR pubblica i dati validati del giorno precedente
    tipicamente entro le 03:00-05:00 UTC).
    """
    _setup_logging()
    if only_sir and only_openmeteo:
        typer.echo("Errore: --only-sir e --only-openmeteo sono mutualmente esclusivi.")
        raise typer.Exit(1)
    if not date:
        date = (datetime.now(tz=UTC) - timedelta(days=1)).strftime("%Y-%m-%d")

    cfg = Path(config_dir)
    locations_all, stations = _load_config(cfg)

    if location:
        unknown = set(location) - set(locations_all)
        if unknown:
            typer.echo(f"Errore: location sconosciute: {sorted(unknown)}")
            raise typer.Exit(1)
        locations = {k: v for k, v in locations_all.items() if k in location}
    else:
        locations = locations_all

    run_sir = not only_openmeteo
    run_om = not only_sir

    typer.echo(f"Daily delta: {date} | location: {list(locations.keys())}")

    if dry_run:
        typer.echo("[dry-run] Nessuna scrittura effettuata.")
        return

    _ping_healthchecks("/start")
    t0 = time.monotonic()
    ok = True
    sir_total = 0
    om_total = 0

    try:
        with DuckDBClient(db_path=db_path) as db:
            db.init_schema()

            if run_sir:
                sir_total = _ingest_sir_historical_range(
                    db, locations, stations, date, date
                )
                logger.info(f"daily SIR: {sir_total} record")

            if run_om:
                results_all = fetch_openmeteo_historical_batch(
                    locations=locations,
                    start_date=date,
                    end_date=date,
                    models=om_model or None,
                )
                for _loc_id, model_results in results_all.items():
                    for _model, records in model_results.items():
                        if records:
                            om_total += db.upsert_forecasts(records)
                logger.info(f"daily Open-Meteo: {om_total} record")

    except Exception as e:
        logger.error(f"daily fallito: {e}")
        _ping_healthchecks("/fail")
        ok = False
        raise typer.Exit(1) from e

    elapsed = time.monotonic() - t0
    _log_scrape("job_daily", "ok" if ok else "fail", rows=sir_total + om_total)
    _ping_healthchecks()
    typer.echo(f"daily completato in {elapsed:.0f}s — SIR:{sir_total} OM:{om_total}")


@app.command("realtime")
def cmd_realtime(
    db_path: Path = _DB_OPT,
    config_dir: str = _CFG_OPT,
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Letture istantanee: SIR actions.php + Netatmo per tutte le location.

    Schedulare ogni 15-30 minuti.
    SIR ha granularità ~15 min; Netatmo aggiorna ogni 10 min circa.
    """
    _setup_logging()
    cfg = Path(config_dir)
    locations, stations = _load_config(cfg)

    if dry_run:
        typer.echo("[dry-run] Nessuna scrittura effettuata.")
        return

    _ping_healthchecks("/start")
    t0 = time.monotonic()
    ok = True
    sir_total = 0
    netatmo_total = 0
    aq_total = 0

    try:
        with DuckDBClient(db_path=db_path) as db:
            db.init_schema()

            # 1a. SIR bulk (TERMO24/IGRO24/ANEMO24/PLUVIO) — 4 call per tutte le stazioni
            all_station_ids = set(_all_sir_station_ids(locations))
            bulk_results = fetch_sir_bulk_realtime(all_station_ids)

            # 1b. SIR per-stazione per le idrometriche (level_m non in bulk)
            idro_ids = _idro_station_ids(locations, stations)
            if idro_ids:
                idro_results = fetch_sir_stations_realtime(sorted(idro_ids))
                for sid, rec in idro_results.items():
                    level = rec.get("level_m")
                    if level is not None:
                        if sid in bulk_results:
                            bulk_results[sid]["level_m"] = level
                        else:
                            bulk_results[sid] = rec

            records_with_loc: list[dict[str, Any]] = []
            for sid, rec in bulk_results.items():
                rec["location_id"] = _location_id_for_station(sid, locations)
                records_with_loc.append(rec)

            if records_with_loc:
                db.upsert_sir_observations(records_with_loc)
                sir_total = len(records_with_loc)
            logger.info(f"realtime SIR: {sir_total} stazioni ({len(idro_ids)} idro per-stazione)")

            # 2. Netatmo — tutte le location
            netatmo_results = fetch_netatmo_all_locations(db, locations)
            netatmo_total = sum(len(v) for v in netatmo_results.values())
            logger.info(f"realtime Netatmo: {netatmo_total} stazioni totali")

            # 3. OpenAQ latest — qualità aria oraria per tutte le location
            aq_results = fetch_openaq_all_locations(locations, mode="latest")
            for _loc_id, records in aq_results.items():
                if records:
                    aq_total += db.upsert_sir_observations(records)
            logger.info(f"realtime OpenAQ: {aq_total} record")

    except Exception as e:
        logger.error(f"realtime fallito: {e}")
        _ping_healthchecks("/fail")
        ok = False
        raise typer.Exit(1) from e

    elapsed = time.monotonic() - t0
    _log_scrape("job_realtime", "ok" if ok else "fail",
                rows=sir_total + netatmo_total + aq_total)
    _ping_healthchecks()
    typer.echo(
        f"realtime completato in {elapsed:.0f}s — "
        f"SIR:{sir_total} Netatmo:{netatmo_total} OpenAQ:{aq_total}"
    )


@app.command("forecasts")
def cmd_forecasts(
    db_path: Path = _DB_OPT,
    config_dir: str = _CFG_OPT,
    forecast_days: int = typer.Option(7, "--days", help="Giorni di forecast (1-16)"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Forecast NWP: Open-Meteo per tutti i modelli e tutte le location.

    Schedulare ogni 6 ore (allineato ai run ECMWF: 00/06/12/18 UTC + lag ~2h).
    Suggerito: 02:00, 08:00, 14:00, 20:00 UTC.
    """
    _setup_logging()
    cfg = Path(config_dir)
    locations, _ = _load_config(cfg)

    if dry_run:
        typer.echo("[dry-run] Nessuna scrittura effettuata.")
        return

    _ping_healthchecks("/start")
    t0 = time.monotonic()
    ok = True
    total = 0

    try:
        with DuckDBClient(db_path=db_path) as db:
            db.init_schema()

            all_results = fetch_openmeteo_all_locations(
                locations=locations,
                forecast_days=forecast_days,
            )

            for _loc_id, model_results in all_results.items():
                for _model, records in model_results.items():
                    if records:
                        n = db.upsert_forecasts(records)
                        total += n

    except Exception as e:
        logger.error(f"forecasts fallito: {e}")
        _ping_healthchecks("/fail")
        ok = False
        raise typer.Exit(1) from e

    elapsed = time.monotonic() - t0
    _log_scrape("job_forecasts", "ok" if ok else "fail", rows=total)
    _ping_healthchecks()
    typer.echo(f"forecasts completato in {elapsed:.0f}s — {total} record inseriti")


if __name__ == "__main__":
    app()
