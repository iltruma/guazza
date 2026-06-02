"""Aggregazione Netatmo realtime → daily (accumulo forward-looking, Sprint 9+).

Le osservazioni Netatmo arrivano realtime (``granularity='realtime'``, ``temp_c``
istantanea). Questo modulo le aggrega in righe daily (``granularity='daily'``)
nella stessa tabella ``observations``, per costruire — dal deploy in poi — uno
storico giornaliero Netatmo.

Non entra nel training del modello: ``features.py`` filtra ``source='sir_toscana'``.
Lo scopo è caratterizzare in Sprint 9+ l'offset tra le SIR di pianura e i
microclimi iperlocali (es. ``casa_cercina`` su Monte Morello, dove l'unica SIR
alla quota giusta — Vaiano — è in un'altra valle). Vedi ``docs/decisions.md``.

Convenzione daily: ``ts`` = mezzanotte del giorno locale Europe/Rome (etichetta,
naive), coerente con le osservazioni SIR daily.
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

from loguru import logger

from guazza.storage import DuckDBClient

# Netatmo è salvato UTC naive (vedi standardizzazione timestamp 2026-05-30): per
# i confini del giorno serve la conversione esplicita al fuso locale.
_LOCAL_TZ = "Europe/Rome"


def aggregate_netatmo_daily(
    db: DuckDBClient,
    target_day: date | None = None,
    min_samples: int = 6,
    dry_run: bool = False,
) -> dict[str, int]:
    """Aggrega il realtime Netatmo in righe daily per (station_id, location_id, giorno locale).

    ``tmin/tmax`` = MIN/MAX(``temp_c``), ``humidity`` = AVG. La precipitazione
    **non** viene aggregata: il realtime Netatmo salva ``rain_1h`` (finestra mobile
    di 60min, campionata ogni ~30min), quindi sommarla raddoppierebbe il totale.
    Un totale giornaliero affidabile richiede una dedup oraria dedicata, rimandata
    al QC Netatmo (Sprint 9+). Anche ``tmax`` è conservata grezza ma è inaffidabile
    (bias solare sui moduli outdoor): va filtrata in fase di QC.

    Args:
        db: client DuckDB (in context manager).
        target_day: giorno locale da aggregare. ``None`` → tutti i giorni con
            realtime Netatmo presente (backfill dell'accumulato).
        min_samples: minimo di campioni ``temp_c`` per giorno/stazione perché
            tmin/tmax siano rappresentativi. Giorni sotto soglia scartati.
        dry_run: se ``True`` non scrive, ritorna solo i conteggi.

    Returns:
        ``{"days": n_giorni, "stations": n_coppie_staz_giorno, "rows": n_upsert}``.
    """
    day_filter = ""
    params: list[Any] = [min_samples]
    if target_day is not None:
        day_filter = "WHERE local_day = ?"
        params = [target_day, min_samples]

    sql = f"""
        WITH local AS (
            SELECT
                station_id,
                location_id,
                temp_c,
                humidity_pct,
                ((ts AT TIME ZONE 'UTC') AT TIME ZONE '{_LOCAL_TZ}')::DATE AS local_day
            FROM observations
            WHERE source = 'netatmo'
              AND granularity = 'realtime'
        )
        SELECT
            station_id,
            location_id,
            local_day,
            MIN(temp_c)       AS tmin_c,
            MAX(temp_c)       AS tmax_c,
            AVG(humidity_pct) AS humidity_pct
        FROM local
        {day_filter}
        GROUP BY station_id, location_id, local_day
        HAVING COUNT(temp_c) >= ?
        ORDER BY local_day, station_id
    """
    rows = db.execute(sql, params).fetchall()

    records: list[dict[str, Any]] = []
    for station_id, location_id, local_day, tmin_c, tmax_c, humidity_pct in rows:
        records.append({
            "source": "netatmo",
            "station_id": station_id,
            "location_id": location_id,
            "ts": datetime.combine(local_day, time.min),
            "granularity": "daily",
            "tmin_c": tmin_c,
            "tmax_c": tmax_c,
            "humidity_pct": humidity_pct,
        })

    n_days = len({r["ts"] for r in records})
    summary = {"days": n_days, "stations": len(records), "rows": 0}

    if dry_run:
        logger.info(f"[dry-run] Netatmo daily: {len(records)} righe da {n_days} giorni")
        return summary

    summary["rows"] = db.upsert_sir_observations(records)
    logger.info(f"Netatmo daily: {summary['rows']} righe upsert da {n_days} giorni")
    return summary
