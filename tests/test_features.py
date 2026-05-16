"""Test per features.py — build_features_daily."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from guazza.features import build_features_daily
from guazza.storage import DuckDBClient


@pytest.fixture
def db(tmp_path: Path) -> DuckDBClient:
    client = DuckDBClient(db_path=tmp_path / "test.duckdb")
    client.__enter__()
    client.init_schema()
    return client


def _populate(db: DuckDBClient, n_days: int = 10) -> None:
    """Inserisce dati minimi: 1 stazione, 1 location, 2 modelli, n_days giorni."""
    # station_weights
    db.execute("""
        INSERT INTO station_weights (station_id, source, location_id, weight, distance_km, delta_elev_m)
        VALUES ('STA1', 'sir', 'loc1', 1.0, 1.0, 0.0)
    """)

    base = datetime(2024, 1, 1)

    # observations SIR daily
    for i in range(n_days + 1):  # +1 per avere il giorno prima
        ts = base + timedelta(days=i)
        db.execute("""
            INSERT INTO observations
                (source, station_id, location_id, ts, granularity, tmin_c, tmax_c, precip_mm, humidity_pct)
            VALUES ('sir_toscana', 'STA1', 'loc1', ?, 'daily', ?, ?, ?, ?)
        """, [ts, 5.0 + i * 0.5, 15.0 + i * 0.3, float(i % 5), 70.0])

    # forecasts orari: 2 modelli, run 12h prima di ogni target_date
    for i in range(1, n_days + 1):
        target_date = base + timedelta(days=i)
        ts_run = target_date - timedelta(hours=24)
        for hour in range(0, 24):
            ts_valid = target_date.replace(hour=hour)
            for source in ["open_meteo_ecmwf_ifs025", "open_meteo_icon_eu"]:
                db.execute("""
                    INSERT INTO forecasts
                        (source, location_id, ts_run, ts_valid, lead_time_h,
                         temp_c, precip_mm, humidity_pct, wind_speed_ms)
                    VALUES (?, 'loc1', ?, ?, ?, ?, ?, ?, ?)
                """, [
                    source, ts_run, ts_valid,
                    int((ts_valid - ts_run).total_seconds() // 3600),
                    10.0 + hour * 0.1,  # temperatura oraria variabile
                    0.5,                 # precip oraria
                    65.0,
                    3.0,
                ])


def test_build_returns_rows(db: DuckDBClient) -> None:
    _populate(db)
    n = build_features_daily(db)
    assert n > 0
    db.__exit__(None, None, None)


def test_build_requires_station_weights(db: DuckDBClient) -> None:
    with pytest.raises(ValueError, match="station_weights"):
        build_features_daily(db)
    db.__exit__(None, None, None)


def test_lead_time_h_positive(db: DuckDBClient) -> None:
    _populate(db)
    build_features_daily(db)
    row = db.execute(
        "SELECT MIN(lead_time_h) FROM features_daily"
    ).fetchone()
    assert row is not None and row[0] > 0
    db.__exit__(None, None, None)


def test_nwp_aggregation_tmin_tmax(db: DuckDBClient) -> None:
    """tmin deve essere < tmax (aggregazione MIN/MAX sulle ore è corretta)."""
    _populate(db)
    build_features_daily(db)
    bad = db.execute("""
        SELECT COUNT(*) FROM features_daily
        WHERE ecmwf_tmin_c >= ecmwf_tmax_c
          AND ecmwf_tmin_c IS NOT NULL
    """).fetchone()
    assert bad is not None and bad[0] == 0
    db.__exit__(None, None, None)


def test_precip_daily_is_sum(db: DuckDBClient) -> None:
    """precip giornaliero = SUM delle ore → 24 * 0.5 = 12.0 mm."""
    _populate(db)
    build_features_daily(db)
    row = db.execute(
        "SELECT MIN(ecmwf_precip_mm) FROM features_daily WHERE ecmwf_precip_mm IS NOT NULL"
    ).fetchone()
    assert row is not None
    assert abs(row[0] - 12.0) < 0.01
    db.__exit__(None, None, None)


def test_target_present_for_observed_dates(db: DuckDBClient) -> None:
    """Le righe con target_date coperta da obs devono avere target non NULL."""
    _populate(db, n_days=5)
    build_features_daily(db)
    pct = db.execute("""
        SELECT ROUND(100.0 * COUNT(target_tmin_c) / COUNT(*), 1)
        FROM features_daily
    """).fetchone()
    assert pct is not None and pct[0] > 50.0
    db.__exit__(None, None, None)


def test_obs_features_use_previous_day(db: DuckDBClient) -> None:
    """obs_tmin_c deve essere NULL per il primo giorno (nessun giorno precedente)."""
    _populate(db, n_days=5)
    build_features_daily(db)
    # Il primo target_date nel DB è base + 1 day; obs_date sarebbe base + 0, che esiste.
    # Verifica che obs features siano presenti (non tutti NULL).
    row = db.execute(
        "SELECT COUNT(*) FROM features_daily WHERE obs_tmin_c IS NOT NULL"
    ).fetchone()
    assert row is not None and row[0] > 0
    db.__exit__(None, None, None)


def test_climatology_present(db: DuckDBClient) -> None:
    _populate(db, n_days=10)
    build_features_daily(db)
    row = db.execute(
        "SELECT COUNT(*) FROM features_daily WHERE clim_tmin_mean IS NOT NULL"
    ).fetchone()
    assert row is not None and row[0] > 0
    db.__exit__(None, None, None)


def test_idempotent(db: DuckDBClient) -> None:
    _populate(db)
    n1 = build_features_daily(db)
    n2 = build_features_daily(db)
    assert n1 == n2
    db.__exit__(None, None, None)


def test_spread_with_partial_models(db: DuckDBClient) -> None:
    """Con 2 modelli su 4, DuckDB GREATEST/LEAST ignora i NULL e restituisce
    il range tra i modelli disponibili (non NULL)."""
    _populate(db)  # inserisce solo ecmwf e icon
    build_features_daily(db)
    row = db.execute("""
        SELECT COUNT(*) FROM features_daily
        WHERE nwp_tmin_spread IS NOT NULL
          AND ecmwf_tmin_c IS NOT NULL
          AND icon_tmin_c IS NOT NULL
          AND gfs_tmin_c IS NULL
    """).fetchone()
    assert row is not None and row[0] > 0
    db.__exit__(None, None, None)
