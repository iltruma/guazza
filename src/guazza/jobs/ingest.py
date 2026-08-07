"""Entry point cron — ingestion dati Guazza.

Due comandi:

  historical  — one-shot: backfill completo SIR CSV + Open-Meteo historical + multilead (2022→oggi)
  realtime    — cron ogni 15-30 min: SIR actions.php + Netatmo + refresh JSON location

L'ingestion giornaliera è in `guazza-review` (finestra [ieri-7, ieri]).
I forecast NWP live e la pipeline ML sono in `guazza-forecast`.

Uso:
    uv run python -m guazza.jobs.ingest historical [--start-date 2022-01-01]
    uv run python -m guazza.jobs.ingest realtime

Variabili d'ambiente:
    DB_PATH           — path file DuckDB (default: /var/lib/guazza/guazza.duckdb)
    CONFIG_DIR        — directory YAML config (default: <repo>/config)
    KUMA_PUSH_URL  — URL push Uptime Kuma (opzionale; se assente, push saltato)
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
from guazza.fetch_netatmo import fetch_netatmo_all_locations
from guazza.fetch_openmeteo import (
    OM_MODELS,
    fetch_openmeteo_historical_batch,
    fetch_openmeteo_multilead_batch,
)
from guazza.fetch_sir import (
    fetch_sir_bulk_realtime,
    fetch_sir_historical,
    fetch_sir_stations_realtime,
)
from guazza.jobs._common import (
    CONFIG_DIR_OPTION,
    DB_OPTION,
    OUTPUT_DIR_OPTION,
    filter_locations,
    job_run,
)
from guazza.output import refresh_realtime_json
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

    total = 0
    for sid, idst, loc_id in tqdm(
        combos,
        desc="SIR historical",
        unit="combo",
        disable=not sys.stderr.isatty(),
    ):
        result = _fetch_one(sid, idst, loc_id)
        if result:
            _, _, filtered = result
            # Upsert per-combo: se il job si interrompe a metà, i dati già
            # scaricati restano persistiti (idempotente al riavvio).
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
    Non schedulare come cron — l'ingestion giornaliera è in `guazza-review`.
    Esempi:
        # Solo Open-Meteo per una location
        historical --only-openmeteo --location casa_campi

        # Solo SIR, intervallo ridotto
        historical --only-sir --start-date 2024-01-01

        # Tutte le sorgenti, tutte le location
        historical --start-date 2022-01-01
    """
    if only_sir and only_openmeteo:
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

    locations_all, stations = load_configs(config_dir)
    locations = filter_locations(locations_all, location)

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

                om_hist_total = 0

                def _on_hist(records: list[dict[str, Any]]) -> None:
                    nonlocal om_hist_total
                    om_hist_total += db.upsert_forecasts(records)

                fetch_openmeteo_historical_batch(
                    locations=locations,
                    start_date=start_date,
                    end_date=om_end_date,
                    models=om_model or None,
                    on_records=_on_hist,
                )
                om_total += om_hist_total
                typer.echo(f"Open-Meteo historical: {om_hist_total} record inseriti")

                typer.echo("\n--- Open-Meteo multilead (batch) ---")
                ml_total = 0

                def _on_ml(records: list[dict[str, Any]]) -> None:
                    nonlocal ml_total
                    ml_total += db.upsert_forecasts(records)

                fetch_openmeteo_multilead_batch(
                    locations=locations,
                    start_date=start_date,
                    end_date=om_end_date,
                    on_records=_on_ml,
                )
                om_total += ml_total
                typer.echo(f"Open-Meteo multilead: {ml_total} record inseriti")

            qc = compute_quality_flags(db)
            typer.echo(f"QC: {qc['total']} flag ({', '.join(f'{k}={v}' for k, v in qc.items() if k != 'total')})")

        stats.rows = sir_total + om_total
        stats.summary = f"SIR:{sir_total} OM:{om_total}"


@app.command("realtime")
def cmd_realtime(
    db_path: Path = DB_OPTION,
    config_dir: Path = CONFIG_DIR_OPTION,
    output_dir: Path = OUTPUT_DIR_OPTION,
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Letture istantanee: SIR actions.php + Netatmo per tutte le location.

    Dopo le scritture realtime, aggiorna `current` dei JSON location esistenti
    (OUTPUT_DIR) senza rifare forecast/features/predict: la pipeline li genera,
    questo job li mantiene freschi tra un run e l'altro.

    Schedulare ogni 15-30 minuti.
    SIR ha granularità ~15 min; Netatmo aggiorna ogni 10 min circa.
    """
    locations, stations = load_configs(config_dir)

    if dry_run:
        typer.echo("[dry-run] Nessuna scrittura effettuata.")
        return

    with job_run("job_realtime") as stats:
        sir_total = 0
        netatmo_total = 0
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

            qc = compute_quality_flags(db)
            logger.info(f"realtime QC: {qc['total']} flag")

            # 3. Refresh JSON location: aggiorna `current` con le osservazioni
            #    appena scritte (stessa connessione), senza rifare la pipeline.
            #    I JSON senza file (mai generati) vengono saltati.
            n_refreshed = 0
            for location_id in locations:
                if refresh_realtime_json(db, location_id, output_dir) is not None:
                    n_refreshed += 1
            logger.info(f"realtime JSON refresh: {n_refreshed}/{len(locations)} location aggiornate")

        stats.rows = sir_total + netatmo_total
        stats.summary = f"SIR:{sir_total} Netatmo:{netatmo_total} JSON:{n_refreshed}"


if __name__ == "__main__":
    app()
