"""Quality control per osservazioni SIR e ARPAT.

Calcola flag di qualità sulla tabella observations e li scrive in quality_flags.
Il ricalcolo è idempotente: DELETE + INSERT ad ogni run in transazione.

Flag implementati:
- spike_tmin         : |tmin_c[t] - tmin_c[t-1]| > SPIKE_TEMP_C (SIR daily)
- spike_tmax         : |tmax_c[t] - tmax_c[t-1]| > SPIKE_TEMP_C (SIR daily)
- inversion_temp     : tmin_c > tmax_c (fisicamente impossibile, SIR daily)
- range_precip_high  : precip_mm > PRECIP_HIGH_MM (SIR daily + realtime)
- range_pm10_high    : pm10_ugm3 > PM10_HIGH_UGM3 (ARPAT)
- range_pm25_high    : pm25_ugm3 > PM25_HIGH_UGM3 (ARPAT)
- range_no2_high     : no2_ugm3 > NO2_HIGH_UGM3 (ARPAT)
- range_o3_high      : o3_ugm3 > O3_HIGH_UGM3 (ARPAT)
"""

from __future__ import annotations

from guazza.storage import DuckDBClient

SPIKE_TEMP_C: float = 10.0
PRECIP_HIGH_MM: float = 150.0
PM10_HIGH_UGM3: float = 200.0
PM25_HIGH_UGM3: float = 100.0
NO2_HIGH_UGM3: float = 400.0
O3_HIGH_UGM3: float = 300.0


def compute_quality_flags(db: DuckDBClient) -> dict[str, int]:
    """Ricalcola tutti i flag di qualità e li scrive in quality_flags.

    Returns:
        Dict con total e breakdown per flag_type.
    """
    db.execute("BEGIN TRANSACTION")
    db.execute("DELETE FROM quality_flags")

    _insert_spike_flags(db, "tmin_c", "spike_tmin")
    _insert_spike_flags(db, "tmax_c", "spike_tmax")
    _insert_inversion_flags(db)
    _insert_range_precip_flags(db)
    _insert_range_arpat_flags(db, "pm10_ugm3", "range_pm10_high", PM10_HIGH_UGM3)
    _insert_range_arpat_flags(db, "pm25_ugm3", "range_pm25_high", PM25_HIGH_UGM3)
    _insert_range_arpat_flags(db, "no2_ugm3", "range_no2_high", NO2_HIGH_UGM3)
    _insert_range_arpat_flags(db, "o3_ugm3", "range_o3_high", O3_HIGH_UGM3)

    rows = db.execute(
        "SELECT flag_type, COUNT(*) FROM quality_flags GROUP BY flag_type ORDER BY flag_type"
    ).fetchall()
    breakdown = {str(r[0]): int(r[1]) for r in rows}
    breakdown["total"] = sum(breakdown.values())
    db.execute("COMMIT")
    return breakdown


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


def _insert_range_arpat_flags(db: DuckDBClient, col: str, flag_type: str, threshold: float) -> None:
    """Flag range per inquinante qualità aria (ARPAT NRT): valore > threshold."""
    db.execute(f"""
        INSERT INTO quality_flags
            (source, station_id, ts, granularity, flag_type, column_name, value, detail)
        SELECT
            source, station_id, ts, granularity,
            '{flag_type}', '{col}',
            {col},
            '{col}=' || ROUND({col}, 1)::VARCHAR || ' ug/m3'
        FROM observations
        WHERE source = 'arpat'
          AND {col} > {threshold}
    """)
