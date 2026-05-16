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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import typer
import yaml
from dotenv import load_dotenv
from loguru import logger

# Carica .env prima di leggere variabili d'ambiente (es. DB_PATH)
_REPO_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(_REPO_ROOT / ".env")

from guazza.fetchers import (  # noqa: E402
    _log_scrape,
    fetch_arpat_all_locations,
    fetch_arpat_bollettini_range,
    fetch_netatmo_all_locations,
    fetch_openmeteo_all_locations,
    fetch_openmeteo_historical_batch,
    fetch_openmeteo_forecast_batch,
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
_SIR_CSV_DELAY = 1.2


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_config(cfg_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Carica locations.yaml e stations.yaml. Restituisce (locations, stations)."""
    with (cfg_dir / "locations.yaml").open() as f:
        locations: dict[str, Any] = yaml.safe_load(f)["locations"]
    with (cfg_dir / "stations.yaml").open() as f:
        stations: dict[str, Any] = yaml.safe_load(f)
    return locations, stations


def _all_sir_station_ids(locations: dict[str, Any]) -> set[str]:
    """Raccoglie tutti gli ID stazione SIR usati (meteo + idro) su tutte le location."""
    ids: set[str] = set()
    for loc in locations.values():
        for sensor_list in loc.get("sir_stations", {}).values():
            ids.update(sensor_list)
        if idro_id := loc.get("sir_idro_id"):
            ids.add(idro_id)
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


def _location_id_for_station(
    station_id: str,
    locations: dict[str, Any],
) -> str:
    """Restituisce il primo location_id che usa questa stazione, o stringa vuota."""
    for loc_id, loc in locations.items():
        for sensor_list in loc.get("sir_stations", {}).values():
            if station_id in sensor_list:
                return loc_id
        if loc.get("sir_idro_id") == station_id:
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

    Per il backfill completo l'API SIR restituisce sempre tutto lo storico
    disponibile — i parametri start/end_date sono usati solo per filtrare
    i record dopo il parsing (il CSV non supporta range).

    Returns: numero totale di record inseriti.
    """
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")

    station_ids = _all_sir_station_ids(locations)
    total = 0

    for i, sid in enumerate(sorted(station_ids)):
        if i > 0:
            time.sleep(_SIR_CSV_DELAY)

        loc_id = _location_id_for_station(sid, locations)
        idst_list = _idst_for_station(sid, stations)

        if not idst_list:
            logger.debug(f"[{sid}] Nessun IDST storico disponibile — skip")
            continue

        for j, idst in enumerate(idst_list):
            if j > 0:
                time.sleep(_SIR_CSV_DELAY)
            try:
                records = fetch_sir_historical(sid, idst, loc_id)
                # Filtra per intervallo richiesto
                filtered = [
                    r for r in records
                    if start_dt <= r["ts"] <= end_dt
                ]
                if filtered:
                    db.upsert_sir_observations(filtered)
                    total += len(filtered)
                    logger.info(
                        f"[{sid}] {idst}: {len(filtered)} righe "
                        f"({start_date}→{end_date})"
                    )
            except Exception as e:
                logger.error(f"[{sid}] {idst} fallito: {e}")
                _log_scrape(f"sir_historical:{sid}:{idst}", "fail", detail=str(e))

    return total


# ── CLI ───────────────────────────────────────────────────────────────────────

app = typer.Typer(
    help="Ingestion dati Guazza — SIR, Netatmo, Open-Meteo.",
    no_args_is_help=True,
)

_DB_OPT = typer.Option(_DEFAULT_DB, "--db", help="Path file DuckDB")
_CFG_OPT = typer.Option(str(_DEFAULT_CFG), "--config-dir", help="Directory YAML config")


def _setup_logging(level: str = "INFO") -> None:
    """Configura loguru per stdout con output JSON strutturato.

    Rimuove il sink di default (testo su stderr) e aggiunge un sink
    su stdout con serialize=True — ogni evento è una riga JSON valida.

    In produzione sarà sufficiente aggiungere un secondo sink su file:
        logger.add("/var/lib/guazza/logs/guazza.jsonl", serialize=True, rotation="1 day")

    Chiamare solo nei comandi CLI, non a livello di modulo, per non
    inquinare l'output dei test pytest.
    """
    logger.remove()  # rimuove il sink di default (stderr, formato testo)
    logger.add(sys.stdout, serialize=True, level=level)


@app.command("historical")
def cmd_historical(
    db_path: Path = _DB_OPT,
    config_dir: str = _CFG_OPT,
    start_date: str = typer.Option("2022-01-01", "--start-date", help="Inizio intervallo YYYY-MM-DD"),
    end_date: str = typer.Option("", "--end-date", help="Fine intervallo YYYY-MM-DD (default: oggi)"),
    only_sir: bool = typer.Option(False, "--only-sir", help="Scarica solo SIR CSV, salta Open-Meteo e ARPAT"),
    only_openmeteo: bool = typer.Option(False, "--only-openmeteo", help="Scarica solo Open-Meteo, salta SIR e ARPAT"),
    only_arpat: bool = typer.Option(False, "--only-arpat", help="Scarica solo ARPAT bollettini, salta SIR e Open-Meteo"),
    location: list[str] | None = typer.Option(None, "--location", help="Limita a questa location (ripetibile)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Stampa cosa farebbe senza scrivere"),
) -> None:
    """Backfill completo: SIR CSV + Open-Meteo historical + ARPAT bollettini (one-shot, lento).

    Da eseguire una volta sola per caricare lo storico di training.
    Non schedulare come cron — usa 'daily' per il delta incrementale.

    Esempi:
        # Solo Open-Meteo per una location
        historical --only-openmeteo --location casa_campi

        # Solo SIR, intervallo ridotto
        historical --only-sir --start-date 2024-01-01

        # Solo ARPAT bollettini dal 2018
        historical --only-arpat --start-date 2018-01-01

        # Tutte le sorgenti, tutte le location
        historical --start-date 2022-01-01
    """
    _setup_logging()
    exclusive = sum([only_sir, only_openmeteo, only_arpat])
    if exclusive > 1:
        typer.echo("Errore: --only-sir, --only-openmeteo, --only-arpat sono mutualmente esclusivi.")
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

    run_sir = not only_openmeteo and not only_arpat
    run_om = not only_sir and not only_arpat
    run_arpat = not only_sir and not only_openmeteo

    typer.echo(f"Historical backfill: {start_date} → {end_date}")
    typer.echo(f"Location: {list(locations.keys())}")
    sorgenti = " ".join(filter(None, [
        "SIR" if run_sir else "",
        "Open-Meteo" if run_om else "",
        "ARPAT" if run_arpat else "",
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
    arpat_total = 0

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
                results_all = fetch_openmeteo_historical_batch(
                    locations=locations,
                    start_date=start_date,
                    end_date=end_date,
                )
                for loc_id, model_results in results_all.items():
                    for _model, records in model_results.items():
                        if records:
                            om_total += db.upsert_forecasts(records)
                typer.echo(f"Open-Meteo historical: {om_total} record inseriti")

            if run_arpat:
                typer.echo("\n--- ARPAT bollettini storico ---")
                for loc_id, loc in locations.items():
                    arpat_stations = loc.get("arpat_stations")
                    if not arpat_stations:
                        logger.debug(f"[{loc_id}] Nessuna stazione ARPAT configurata — skip")
                        continue
                    records = fetch_arpat_bollettini_range(
                        location_id=loc_id,
                        arpat_stations=arpat_stations,
                        start_date=start_date,
                        end_date=end_date,
                    )
                    if records:
                        arpat_total += db.upsert_sir_observations(records)
                typer.echo(f"ARPAT bollettini: {arpat_total} record inseriti")

    except Exception as e:
        logger.error(f"historical fallito: {e}")
        _ping_healthchecks("/fail")
        ok = False
        raise typer.Exit(1) from e

    elapsed = time.monotonic() - t0
    _log_scrape("job_historical", "ok" if ok else "fail",
                rows=sir_total + om_total + arpat_total if ok else None)
    _ping_healthchecks()
    typer.echo(f"\nCompletato in {elapsed:.0f}s — SIR:{sir_total} OM:{om_total} ARPAT:{arpat_total}")


@app.command("daily")
def cmd_daily(
    db_path: Path = _DB_OPT,
    config_dir: str = _CFG_OPT,
    date: str = typer.Option("", "--date", help="Giorno da caricare YYYY-MM-DD (default: ieri)"),
    only_sir: bool = typer.Option(False, "--only-sir", help="Scarica solo SIR CSV, salta Open-Meteo"),
    only_openmeteo: bool = typer.Option(False, "--only-openmeteo", help="Scarica solo Open-Meteo, salta SIR"),
    location: list[str] | None = typer.Option(None, "--location", help="Limita a questa location (ripetibile)"),
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
    arpat_total = 0

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
                )
                for loc_id, model_results in results_all.items():
                    for _model, records in model_results.items():
                        if records:
                            om_total += db.upsert_forecasts(records)
                logger.info(f"daily Open-Meteo: {om_total} record")

            # ARPAT bollettini giornalieri (PM10, PM2.5)
            arpat_results = fetch_arpat_all_locations(locations, mode="bollettini", date=date)
            for _loc_id, records in arpat_results.items():
                if records:
                    arpat_total += db.upsert_sir_observations(records)
            logger.info(f"daily ARPAT bollettini: {arpat_total} record")

    except Exception as e:
        logger.error(f"daily fallito: {e}")
        _ping_healthchecks("/fail")
        ok = False
        raise typer.Exit(1) from e

    elapsed = time.monotonic() - t0
    _log_scrape("job_daily", "ok" if ok else "fail", rows=sir_total + om_total + arpat_total)
    _ping_healthchecks()
    typer.echo(f"daily completato in {elapsed:.0f}s — SIR:{sir_total} OM:{om_total} ARPAT:{arpat_total}")


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
    locations, _ = _load_config(cfg)

    if dry_run:
        typer.echo("[dry-run] Nessuna scrittura effettuata.")
        return

    _ping_healthchecks("/start")
    t0 = time.monotonic()
    ok = True
    sir_total = 0
    netatmo_total = 0
    arpat_total = 0

    try:
        with DuckDBClient(db_path=db_path) as db:
            db.init_schema()

            # 1. SIR realtime — tutte le stazioni attive (deduplicate)
            all_station_ids = sorted(_all_sir_station_ids(locations))
            results = fetch_sir_stations_realtime(all_station_ids, delay=1.0)

            records_with_loc: list[dict[str, Any]] = []
            for sid, rec in results.items():
                rec["location_id"] = _location_id_for_station(sid, locations)
                records_with_loc.append(rec)

            if records_with_loc:
                db.upsert_sir_observations(records_with_loc)
                sir_total = len(records_with_loc)
            logger.info(f"realtime SIR: {sir_total} stazioni")

            # 2. Netatmo — tutte le location
            netatmo_results = fetch_netatmo_all_locations(db, locations)
            netatmo_total = sum(len(v) for v in netatmo_results.values())
            logger.info(f"realtime Netatmo: {netatmo_total} stazioni totali")

            # 3. ARPAT NRT — valori orari NO2/O3
            arpat_results = fetch_arpat_all_locations(locations, mode="nrt")
            for _loc_id, records in arpat_results.items():
                if records:
                    arpat_total += db.upsert_sir_observations(records)
            logger.info(f"realtime ARPAT NRT: {arpat_total} record")

    except Exception as e:
        logger.error(f"realtime fallito: {e}")
        _ping_healthchecks("/fail")
        ok = False
        raise typer.Exit(1) from e

    elapsed = time.monotonic() - t0
    _log_scrape("job_realtime", "ok" if ok else "fail",
                rows=sir_total + netatmo_total + arpat_total)
    _ping_healthchecks()
    typer.echo(
        f"realtime completato in {elapsed:.0f}s — "
        f"SIR:{sir_total} Netatmo:{netatmo_total} ARPAT:{arpat_total}"
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
