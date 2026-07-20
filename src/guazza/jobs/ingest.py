"""Entry point cron — ingestion dati Guazza.

Quattro comandi con schedulazioni diverse:

  historical  — one-shot: backfill completo SIR CSV + Open-Meteo historical + multilead (2022→oggi)
  daily       — cron 1×/giorno: delta di ieri (SIR CSV + OM historical lead=0 + OM multilead + Netatmo daily)
  realtime    — cron ogni 15-30 min: SIR actions.php + Netatmo + ARPAT
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

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import typer
from loguru import logger
from tqdm import tqdm

from guazza._logging import log_scrape, setup_logging
from guazza.fetch_arpat import (
    fetch_arpat_all_locations,
    fetch_arpat_bollettino_all_locations,
)
from guazza.fetch_netatmo import fetch_netatmo_all_locations
from guazza.fetch_openmeteo import (
    OM_MODELS,
    fetch_openmeteo_all_locations,
    fetch_openmeteo_historical_batch,
    fetch_openmeteo_multilead_batch,
)
from guazza.fetch_sir import (
    fetch_sir_bulk_realtime,
    fetch_sir_historical,
    fetch_sir_stations_realtime,
)
from guazza.jobs._common import CONFIG_DIR_OPTION, DB_OPTION, job_run
from guazza.netatmo_daily import aggregate_netatmo_daily
from guazza.qc import compute_quality_flags
from guazza.storage import DuckDBClient
from guazza.weights import load_configs

# ── Costanti ─────────────────────────────────────────────────────────────────

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


# ── Helpers ───────────────────────────────────────────────────────────────────

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

    Fetch sequenziale: www.sir.toscana.it serializza le connessioni per IP
    (~3s/request), il parallelismo non aumenta il throughput.

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
                log_scrape(f"sir_historical:{sid}:{idst}", "ok", rows=len(filtered))
                return (sid, idst, filtered)
            return None
        except httpx.HTTPStatusError as e:
            logger.opt(exception=True).error(
                f"SIR historical [{sid}] {idst}: HTTP {e.response.status_code} "
                f"su {e.request.url}"
            )
            log_scrape(f"sir_historical:{sid}:{idst}", "fail", detail=f"HTTP {e.response.status_code}")
            return None
        except Exception as e:
            logger.opt(exception=True).error(f"SIR historical [{sid}] {idst} fallito: {e}")
            log_scrape(f"sir_historical:{sid}:{idst}", "fail", detail=str(e))
            return None

    results: list[tuple[str, str, list[dict[str, Any]]]] = []
    for sid, idst, loc_id in tqdm(
        combos,
        desc="SIR historical",
        unit="combo",
        disable=not sys.stderr.isatty(),
    ):
        result = _fetch_one(sid, idst, loc_id)
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


@app.callback()
def _callback() -> None:
    setup_logging()


@app.command("historical")
def cmd_historical(
    db_path: Path = DB_OPTION,
    config_dir: Path = CONFIG_DIR_OPTION,
    start_date: str = typer.Option("2022-01-01", "--start-date", help="Inizio intervallo YYYY-MM-DD"),
    end_date: str = typer.Option("", "--end-date", help="Fine intervallo YYYY-MM-DD (default: oggi)"),
    only_sir: bool = typer.Option(False, "--only-sir", help="Scarica solo SIR CSV, salta Open-Meteo"),
    only_openmeteo: bool = typer.Option(False, "--only-openmeteo", help="Scarica solo Open-Meteo, salta SIR"),
    location: list[str] | None = typer.Option(None, "--location", help="Limita a questa location (ripetibile)"),
    om_model: list[str] | None = typer.Option(None, "--om-model", help="Limita Open-Meteo a questo modello (ripetibile). Es: --om-model italia_meteo_arpae_icon_2i"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Stampa cosa farebbe senza scrivere"),
) -> None:
    """Backfill completo: SIR CSV + Open-Meteo historical (lead=0) + multilead (lead 24-168h).

    Da eseguire una volta sola per caricare lo storico di training completo.
    Non schedulare come cron — usa 'daily' per il delta incrementale.
    La qualità aria (ARPAT NRT) non ha storico scaricabile — usa 'realtime'.

    Esempi:
        # Solo Open-Meteo per una location
        historical --only-openmeteo --location casa_campi

        # Solo SIR, intervallo ridotto
        historical --only-sir --start-date 2024-01-01

        # Tutte le sorgenti, tutte le location
        historical --start-date 2022-01-01
    """
    exclusive = sum([only_sir, only_openmeteo])
    if exclusive > 1:
        typer.echo("Errore: --only-sir e --only-openmeteo sono mutualmente esclusivi.")
        raise typer.Exit(1)
    if om_model:
        unknown_models = set(om_model) - set(OM_MODELS)
        if unknown_models:
            typer.echo(f"Errore: modelli sconosciuti: {sorted(unknown_models)}")
            typer.echo(f"Disponibili: {OM_MODELS}")
            raise typer.Exit(1)
    if not end_date:
        end_date = datetime.now(tz=UTC).strftime("%Y-%m-%d")

    locations_all, stations = load_configs(Path(config_dir))

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

    run_sir = not only_openmeteo
    run_om = not only_sir

    typer.echo(f"Historical backfill: {start_date} → {end_date}")
    typer.echo(f"Location: {list(locations.keys())}")
    sorgenti = " ".join(filter(None, [
        "SIR" if run_sir else "",
        "Open-Meteo" if run_om else "",
    ]))
    typer.echo(f"Sorgenti: {sorgenti}")
    if run_sir:
        typer.echo(f"Stazioni SIR: {len(_all_sir_station_ids(locations))}")

    if dry_run:
        typer.echo("[dry-run] Nessuna scrittura effettuata.")
        return

    with job_run("job_historical") as stats:
        sir_total = 0
        om_total = 0
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
                for model_results in results_all.values():
                    for records in model_results.values():
                        if records:
                            om_total += db.upsert_forecasts(records)
                typer.echo(f"Open-Meteo historical: {om_total} record inseriti")

                typer.echo("\n--- Open-Meteo multilead (batch) ---")
                ml_results = fetch_openmeteo_multilead_batch(
                    locations=locations,
                    start_date=start_date,
                    end_date=om_end_date,
                )
                ml_total = 0
                for model_results in ml_results.values():
                    for records in model_results.values():
                        if records:
                            ml_total += db.upsert_forecasts(records)
                om_total += ml_total
                typer.echo(f"Open-Meteo multilead: {ml_total} record inseriti")

            qc = compute_quality_flags(db)
            typer.echo(f"QC: {qc['total']} flag ({', '.join(f'{k}={v}' for k, v in qc.items() if k != 'total')})")

        stats.rows = sir_total + om_total
        stats.summary = f"SIR:{sir_total} OM:{om_total}"



@app.command("daily")
def cmd_daily(
    db_path: Path = DB_OPTION,
    config_dir: Path = CONFIG_DIR_OPTION,
    date: str = typer.Option("", "--date", help="Giorno da caricare YYYY-MM-DD (default: ieri)"),
    only_sir: bool = typer.Option(False, "--only-sir", help="Scarica solo SIR CSV, salta Open-Meteo"),
    only_openmeteo: bool = typer.Option(False, "--only-openmeteo", help="Scarica solo Open-Meteo, salta SIR"),
    location: list[str] | None = typer.Option(None, "--location", help="Limita a questa location (ripetibile)"),
    om_model: list[str] | None = typer.Option(None, "--om-model", help="Limita Open-Meteo a questo modello (ripetibile)"),
    netatmo_all: bool = typer.Option(False, "--netatmo-all", help="Backfill Netatmo daily su tutti i giorni accumulati (invece del solo giorno indicato)"),
    netatmo_min_samples: int = typer.Option(6, "--netatmo-min-samples", help="Minimo campioni temp_c per aggregazione Netatmo daily"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Delta incrementale giornaliero: SIR CSV + Open-Meteo historical (lead=0) + multilead (lead 24-168h) + Netatmo daily.

    Schedulare a ~06:00 UTC (SIR pubblica i dati validati del giorno precedente
    tipicamente entro le 03:00-05:00 UTC).

    Usa --netatmo-all per il backfill iniziale di tutti i giorni Netatmo accumulati.
    """
    if only_sir and only_openmeteo:
        typer.echo("Errore: --only-sir e --only-openmeteo sono mutualmente esclusivi.")
        raise typer.Exit(1)
    if not date:
        date = (datetime.now(tz=UTC) - timedelta(days=1)).strftime("%Y-%m-%d")

    locations_all, stations = load_configs(Path(config_dir))

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

    with job_run("job_daily") as stats:
        sir_total = 0
        om_total = 0
        with DuckDBClient(db_path=db_path) as db:
            db.init_schema()

            if run_sir:
                sir_total = _ingest_sir_historical_range(
                    db, locations, stations, date, date
                )
                logger.info(f"daily SIR: {sir_total} record")

            if run_om:
                # OM historical: lead=0 (best-estimate retroattivo)
                results_all = fetch_openmeteo_historical_batch(
                    locations=locations,
                    start_date=date,
                    end_date=date,
                    models=om_model or None,
                )
                for model_results in results_all.values():
                    for records in model_results.values():
                        if records:
                            om_total += db.upsert_forecasts(records)
                logger.info(f"daily Open-Meteo historical: {om_total} record")

                # OM multilead: lead 24-168h (cosa i modelli prevedevano per ieri)
                ml_results = fetch_openmeteo_multilead_batch(
                    locations=locations,
                    start_date=date,
                    end_date=date,
                )
                ml_total = 0
                for model_results in ml_results.values():
                    for records in model_results.values():
                        if records:
                            ml_total += db.upsert_forecasts(records)
                om_total += ml_total
                logger.info(f"daily Open-Meteo multilead: {ml_total} record")

            # Aggregazione Netatmo: backfill completo con --netatmo-all,
            # altrimenti solo il giorno indicato.
            netatmo_target = None if netatmo_all else datetime.strptime(date, "%Y-%m-%d").date()
            nd = aggregate_netatmo_daily(db, target_day=netatmo_target, min_samples=netatmo_min_samples)
            logger.info(f"daily Netatmo: {nd['rows']} record")

            qc = compute_quality_flags(db)
            logger.info(f"daily QC: {qc['total']} flag")

        stats.rows = sir_total + om_total
        stats.summary = f"SIR:{sir_total} OM:{om_total}"


@app.command("realtime")
def cmd_realtime(
    db_path: Path = DB_OPTION,
    config_dir: Path = CONFIG_DIR_OPTION,
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Letture istantanee: SIR actions.php + Netatmo per tutte le location.

    Schedulare ogni 15-30 minuti.
    SIR ha granularità ~15 min; Netatmo aggiorna ogni 10 min circa.
    """
    locations, stations = load_configs(Path(config_dir))

    if dry_run:
        typer.echo("[dry-run] Nessuna scrittura effettuata.")
        return

    with job_run("job_realtime") as stats:
        sir_total = 0
        netatmo_total = 0
        aq_total = 0
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

            # 3. ARPAT NRT — qualità aria oraria per tutte le location
            aq_results = fetch_arpat_all_locations(locations)
            for records in aq_results.values():
                if records:
                    aq_total += db.upsert_sir_observations(records)

            # 4. ARPAT bollettino — PM10/PM2.5 giornaliero (latenza ~2gg, unico endpoint regionale)
            boll_records = fetch_arpat_bollettino_all_locations(locations)
            if boll_records:
                aq_total += db.upsert_sir_observations(boll_records)
            logger.info(f"realtime ARPAT: {aq_total} record ({len(boll_records)} bollettino PM10/PM2.5)")

            qc = compute_quality_flags(db)
            logger.info(f"realtime QC: {qc['total']} flag")

        stats.rows = sir_total + netatmo_total + aq_total
        stats.summary = f"SIR:{sir_total} Netatmo:{netatmo_total} ARPAT:{aq_total}"


@app.command("forecasts")
def cmd_forecasts(
    db_path: Path = DB_OPTION,
    config_dir: Path = CONFIG_DIR_OPTION,
    forecast_days: int = typer.Option(7, "--days", help="Giorni di forecast (1-16)"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Forecast NWP: Open-Meteo per tutti i modelli e tutte le location.

    Schedulare ogni 6 ore (allineato ai run ECMWF: 00/06/12/18 UTC + lag ~2h).
    Suggerito: 02:00, 08:00, 14:00, 20:00 UTC.
    """
    locations, _ = load_configs(Path(config_dir))

    if dry_run:
        typer.echo("[dry-run] Nessuna scrittura effettuata.")
        return

    with job_run("job_forecasts") as stats:
        total = 0
        with DuckDBClient(db_path=db_path) as db:
            db.init_schema()

            all_results = fetch_openmeteo_all_locations(
                locations=locations,
                forecast_days=forecast_days,
            )

            for model_results in all_results.values():
                for records in model_results.values():
                    if records:
                        total += db.upsert_forecasts(records)

        stats.rows = total
        stats.summary = f"{total} record inseriti"


if __name__ == "__main__":
    app()
