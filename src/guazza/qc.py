"""Quality control per osservazioni SIR.

Calcola flag di qualità sulla tabella observations e li scrive in quality_flags.
Il ricalcolo è idempotente: DELETE + INSERT ad ogni run in transazione.

Flag implementati:
- spike_tmin         : |tmin_c[t] - tmin_c[t-1]| > SPIKE_TEMP_C (SIR daily)
- spike_tmax         : |tmax_c[t] - tmax_c[t-1]| > SPIKE_TEMP_C (SIR daily)
- inversion_temp     : tmin_c > tmax_c (fisicamente impossibile, SIR daily)
- range_precip_high  : precip_mm > PRECIP_HIGH_MM (SIR daily + realtime)
"""

from __future__ import annotations

from guazza.storage import DuckDBClient

SPIKE_TEMP_C: float = 10.0
PRECIP_HIGH_MM: float = 150.0


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
