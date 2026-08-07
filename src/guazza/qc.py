"""Quality control per osservazioni SIR e Netatmo.

Calcola flag di qualità sulla tabella observations e li scrive in quality_flags.
Il ricalcolo è idempotente: DELETE + INSERT ad ogni run in transazione.

Flag implementati:
- spike_tmin         : |tmin_c[t] - tmin_c[t-1]| > SPIKE_TEMP_C (SIR daily)
- spike_tmax         : |tmax_c[t] - tmax_c[t-1]| > SPIKE_TEMP_C (SIR daily)
- inversion_temp     : tmin_c > tmax_c (fisicamente impossibile, SIR daily)
- range_precip_high  : precip_mm > PRECIP_HIGH_MM (SIR daily + realtime)
- spike_realtime     : |temp_c[t] - temp_c[t-1]| > SPIKE_REALTIME_C entro 90 min
                       (SIR + Netatmo realtime; gap > 90 min non è spike)
- stall_sensor       : temp_c costante (arrotondata a 0.1°C) per >= STALL_MINUTES min
                       (SIR + Netatmo realtime; gap > 90 min rompe la run)
- bias_solar         : Netatmo realtime in fascia 10-17 ora locale con cielo sereno
                       (weather_code modale NWP in {0,1}; conservative: senza forecast → nessun flag)
"""

from __future__ import annotations

from guazza.db_queries import _modal_weather_code
from guazza.storage import DuckDBClient

SPIKE_TEMP_C: float = 10.0
PRECIP_HIGH_MM: float = 150.0

SPIKE_REALTIME_C: float = 8.0
STALL_MINUTES: int = 180
STALL_ROUND: int = 1
SOLAR_START_HOUR: int = 10
SOLAR_END_HOUR: int = 17
SOLAR_WEATHER_CODES: set[int] = {0, 1}


def compute_quality_flags(db: DuckDBClient) -> dict[str, int]:
    """Ricalcola tutti i flag di qualità e li scrive in quality_flags.

    Returns:
        Dict con total e breakdown per flag_type.
    """
    db.execute("BEGIN TRANSACTION")
    try:
        db.execute("DELETE FROM quality_flags")
        _insert_spike_flags(db, "tmin_c", "spike_tmin")
        _insert_spike_flags(db, "tmax_c", "spike_tmax")
        _insert_inversion_flags(db)
        _insert_range_precip_flags(db)
        _insert_spike_realtime_flags(db)
        _insert_stall_flags(db)
        _insert_bias_solar_flags(db)
        rows = db.execute(
            "SELECT flag_type, COUNT(*) FROM quality_flags GROUP BY flag_type ORDER BY flag_type"
        ).fetchall()
        breakdown = {str(r[0]): int(r[1]) for r in rows}
        breakdown["total"] = sum(breakdown.values())
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    return breakdown


def _insert_spike_realtime_flags(db: DuckDBClient) -> None:
    """Flag spike realtime: salto temp_c > SPIKE_REALTIME_C in ≤ 90 min (SIR + Netatmo).

    Un gap temporale > 90 min NON è uno spike (dati mancanti tra due letture).
    """
    db.execute(f"""
        INSERT INTO quality_flags
            (source, station_id, ts, granularity, flag_type, column_name, value, detail)
        SELECT
            source, station_id, ts, granularity,
            'spike_realtime', 'temp_c',
            ABS(temp_c - prev_temp_c) AS delta,
            'delta=' || ROUND(ABS(temp_c - prev_temp_c), 2)::VARCHAR
              || ' prev=' || ROUND(prev_temp_c, 2)::VARCHAR
              || ' curr=' || ROUND(temp_c, 2)::VARCHAR
        FROM (
            SELECT
                source, station_id, ts, granularity, temp_c,
                LAG(temp_c) OVER (
                    PARTITION BY source, station_id ORDER BY ts
                ) AS prev_temp_c,
                LAG(ts) OVER (
                    PARTITION BY source, station_id ORDER BY ts
                ) AS prev_ts
            FROM observations
            WHERE granularity = 'realtime'
              AND source IN ('sir_toscana', 'netatmo')
              AND temp_c IS NOT NULL
        ) sub
        WHERE prev_temp_c IS NOT NULL
          AND prev_ts IS NOT NULL
          AND ts - prev_ts <= INTERVAL 90 MINUTES
          AND ABS(temp_c - prev_temp_c) > {SPIKE_REALTIME_C}
    """)


def _insert_stall_flags(db: DuckDBClient) -> None:
    """Flag sensore bloccato: temp_c costante (arrot. a STALL_ROUND decimale) per >= STALL_MINUTES min.

    Un gap temporale > 90 min rompe la run (reimposta il gruppo).
    Viene flaggata l'INTERA run (tutti i campioni) se la sua durata totale
    supera la soglia — non solo la coda: l'esclusione nel dataset del correttore
    deve coprire tutto il periodo di stallo.
    """
    db.execute(f"""
        INSERT INTO quality_flags
            (source, station_id, ts, granularity, flag_type, column_name, value, detail)
        SELECT
            source, station_id, ts, granularity,
            'stall_sensor', 'temp_c',
            temp_c,
            'run_total_min=' || date_diff('minute', run_start, run_end)::VARCHAR
              || ' temp=' || ROUND(temp_c, 1)::VARCHAR
        FROM (
            SELECT
                source, station_id, ts, granularity, temp_c, grp,
                MIN(ts) OVER (
                    PARTITION BY source, station_id, grp
                ) AS run_start,
                MAX(ts) OVER (
                    PARTITION BY source, station_id, grp
                ) AS run_end
            FROM (
                SELECT
                    source, station_id, ts, granularity, temp_c,
                    SUM(CAST(is_change AS INT)) OVER (
                        PARTITION BY source, station_id ORDER BY ts
                    ) AS grp
                FROM (
                    SELECT
                        source, station_id, ts, granularity, temp_c,
                        COALESCE(
                            ROUND(temp_c, {STALL_ROUND}) <>
                                ROUND(prev_temp, {STALL_ROUND})
                            OR prev_gap_big,
                            FALSE
                        ) AS is_change
                    FROM (
                        SELECT
                            source, station_id, ts, granularity, temp_c,
                            LAG(temp_c) OVER (
                                PARTITION BY source, station_id ORDER BY ts
                            ) AS prev_temp,
                            (ts - LAG(ts) OVER (
                                PARTITION BY source, station_id ORDER BY ts
                            )) > INTERVAL 90 MINUTES AS prev_gap_big
                        FROM observations
                        WHERE granularity = 'realtime'
                          AND source IN ('sir_toscana', 'netatmo')
                          AND temp_c IS NOT NULL
                    ) lag_sub
                ) change_sub
            ) grp_sub
        ) run_sub
        WHERE date_diff('minute', run_start, run_end) >= {STALL_MINUTES}
    """)


def _insert_bias_solar_flags(db: DuckDBClient) -> None:
    """Flag irraggiamento solare Netatmo: realtime ore 10-17 locali con cielo sereno (wc modale NWP in {{0,1}}).

    Conservative: se non ci sono forecast per (location, giorno, ora) → nessun flag.
    La moda del weather_code è calcolata in Python (pattern output.py hour_wc_modal).
    """
    # Step 1: weather_code per (location, local_date, local_hour) — run più recente per (source, ts_valid)
    # Bound temporale (P4, review oracle): servono solo i weather_code delle date con
    # osservazioni Netatmo realtime flaggabili — la subquery evita di caricare
    # l'intero storico forecasts a ogni run del batch QC.
    wc_rows = db.execute("""
        SELECT location_id, local_date, local_hour, weather_code
        FROM (
            SELECT
                location_id,
                CAST(ts_valid AT TIME ZONE 'UTC' AT TIME ZONE 'Europe/Rome' AS DATE) AS local_date,
                HOUR(ts_valid AT TIME ZONE 'UTC' AT TIME ZONE 'Europe/Rome') AS local_hour,
                weather_code
            FROM forecasts
            WHERE weather_code IS NOT NULL
              AND CAST(ts_valid AT TIME ZONE 'UTC' AT TIME ZONE 'Europe/Rome' AS DATE) >= (
                  SELECT MIN(CAST(ts AT TIME ZONE 'UTC' AT TIME ZONE 'Europe/Rome' AS DATE))
                  FROM observations
                  WHERE granularity = 'realtime'
                    AND source = 'netatmo'
                    AND temp_c IS NOT NULL
              )
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY source, location_id, ts_valid
                ORDER BY ts_run DESC
            ) = 1
        ) latest
    """).fetchall()

    if not wc_rows:
        return

    # Step 2: moda per (location_id, local_date, local_hour) in Python
    code_lists: dict[tuple[str, str, int], list[int]] = {}
    for loc_id, ldate, lhour, wc in wc_rows:
        key = (str(loc_id), str(ldate), int(lhour))
        code_lists.setdefault(key, []).append(int(wc))

    # Mantieni solo chiavi dove la moda è in SOLAR_WEATHER_CODES e ora locale in [10,17)
    solar_keys: list[tuple[str, str, int]] = []
    for key, codes in code_lists.items():
        loc_id, ldate, lhour = key
        if lhour < SOLAR_START_HOUR or lhour >= SOLAR_END_HOUR:
            continue
        modal = _modal_weather_code(codes)
        if modal is not None and modal in SOLAR_WEATHER_CODES:
            solar_keys.append(key)

    if not solar_keys:
        return

    # Step 3: flag le osservazioni Netatmo realtime che coincidono con (location, local_date, local_hour) sereno
    # Registra le chiavi serene come relazione DuckDB per il JOIN
    import pandas as pd  # noqa: PLC0415
    solar_df = pd.DataFrame(
        [(loc, d, h) for loc, d, h in solar_keys],
        columns=["location_id", "local_date", "local_hour"],
    )
    solar_df["local_date"] = solar_df["local_date"].astype(str)
    db.register_df("_qc_solar", solar_df)
    try:
        db.execute(f"""
            INSERT INTO quality_flags
                (source, station_id, ts, granularity, flag_type, column_name, value, detail)
            SELECT
                o.source, o.station_id, o.ts, o.granularity,
                'bias_solar', 'temp_c',
                o.temp_c,
                'temp=' || ROUND(o.temp_c, 2)::VARCHAR
                  || ' solar_hour=' || HOUR(o.ts AT TIME ZONE 'UTC' AT TIME ZONE 'Europe/Rome')::VARCHAR
            FROM (
                SELECT
                    source, station_id, location_id, ts, granularity, temp_c,
                    CAST(ts AT TIME ZONE 'UTC' AT TIME ZONE 'Europe/Rome' AS DATE)::VARCHAR AS local_date_str,
                    HOUR(ts AT TIME ZONE 'UTC' AT TIME ZONE 'Europe/Rome') AS local_hour_int
                FROM observations
                WHERE source = 'netatmo'
                  AND granularity = 'realtime'
                  AND temp_c IS NOT NULL
                  AND HOUR(ts AT TIME ZONE 'UTC' AT TIME ZONE 'Europe/Rome') >= {SOLAR_START_HOUR}
                  AND HOUR(ts AT TIME ZONE 'UTC' AT TIME ZONE 'Europe/Rome') < {SOLAR_END_HOUR}
            ) o
            JOIN _qc_solar s
              ON o.location_id = s.location_id
             AND o.local_date_str = s.local_date
             AND o.local_hour_int = s.local_hour
        """)
    finally:
        db.unregister_df("_qc_solar")


def _insert_spike_flags(db: DuckDBClient, col: str, flag_type: str) -> None:
    """Flag spike su colonna temperatura: |val[t] - val[t-1]| > SPIKE_TEMP_C."""
    db.execute(f"""
        INSERT INTO quality_flags
            (source, station_id, ts, granularity, flag_type, column_name, value, detail)
        SELECT
            source, station_id, ts, granularity,
            '{flag_type}', '{col}',
            ABS({col} - prev_val) AS delta,
            'delta=' || ROUND(ABS({col} - prev_val), 2)::VARCHAR
              || ' prev=' || ROUND(prev_val, 2)::VARCHAR
              || ' curr=' || ROUND({col}, 2)::VARCHAR
        FROM (
            SELECT
                source, station_id, ts, granularity, {col},
                LAG({col}) OVER (
                    PARTITION BY source, station_id, granularity
                    ORDER BY ts
                ) AS prev_val
            FROM observations
            WHERE source = 'sir_toscana'
              AND granularity = 'daily'
              AND {col} IS NOT NULL
        ) sub
        WHERE prev_val IS NOT NULL
          AND ABS({col} - prev_val) > {SPIKE_TEMP_C}
    """)


def _insert_inversion_flags(db: DuckDBClient) -> None:
    """Flag inversione termica: tmin_c > tmax_c."""
    db.execute("""
        INSERT INTO quality_flags
            (source, station_id, ts, granularity, flag_type, column_name, value, detail)
        SELECT
            source, station_id, ts, granularity,
            'inversion_temp', 'tmin_c',
            tmin_c,
            'tmin=' || ROUND(tmin_c, 2)::VARCHAR
              || ' tmax=' || ROUND(tmax_c, 2)::VARCHAR
        FROM observations
        WHERE source = 'sir_toscana'
          AND granularity = 'daily'
          AND tmin_c IS NOT NULL
          AND tmax_c IS NOT NULL
          AND tmin_c > tmax_c
    """)


def _insert_range_precip_flags(db: DuckDBClient) -> None:
    """Flag precipitazione estrema: precip_mm > PRECIP_HIGH_MM (daily + realtime)."""
    db.execute(f"""
        INSERT INTO quality_flags
            (source, station_id, ts, granularity, flag_type, column_name, value, detail)
        SELECT
            source, station_id, ts, granularity,
            'range_precip_high', 'precip_mm',
            precip_mm,
            'precip=' || ROUND(precip_mm, 1)::VARCHAR || 'mm'
        FROM observations
        WHERE source = 'sir_toscana'
          AND granularity IN ('daily', 'realtime')
          AND precip_mm > {PRECIP_HIGH_MM}
    """)
