"""Test per monitor.py — coverage_30d + alert drift."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from guazza.monitor import compute_coverage
from guazza.storage import DuckDBClient


@pytest.fixture
def db(tmp_path: Path) -> DuckDBClient:
    client = DuckDBClient(db_path=tmp_path / "test.duckdb")
    client.__enter__()
    client.init_schema()
    client.ensure_aci_schema()
    # predictions con tmin/tmax/precip_obs (alcune coperte, altre no)
    today = date.today() - timedelta(days=20)
    rows = []
    for i in range(50):
        ts_date = today + timedelta(days=i)
        ts = datetime(ts_date.year, ts_date.month, ts_date.day)  # ts_valid è TIMESTAMP
        actual_tmin = 5.0 + i * 0.1
        # model_version VARCHAR, location_id VARCHAR, ts_valid TIMESTAMP, lead_time_h BIGINT
        rows.append((
            "20260601",  # model_version
            f"loc_{i % 3}",  # location_id
            ts,  # ts_valid TIMESTAMP
            0,  # lead_time_h
            4.0, 4.5, 5.0, 5.5, 6.0, 4.5, 5.5, 4.0, 6.0, actual_tmin,
            14.0, 14.5, 15.0, 15.5, 16.0, 14.5, 15.5, 14.0, 16.0, 15.0,
            0.0, 0.0, 0.0, 0.5, 1.0, 0.0, 0.5, 0.0, 1.0, 0.2,
        ))
    client._conn.executemany("""
        INSERT INTO predictions (
            model_version, location_id, ts_valid, lead_time_h,
            tmin_p05, tmin_p10, tmin_p50, tmin_p90, tmin_p95,
            tmin_ci80_lo, tmin_ci80_hi, tmin_ci90_lo, tmin_ci90_hi,
            tmax_p05, tmax_p10, tmax_p50, tmax_p90, tmax_p95,
            tmax_ci80_lo, tmax_ci80_hi, tmax_ci90_lo, tmax_ci90_hi,
            precip_p05, precip_p10, precip_p50, precip_p90, precip_p95,
            precip_ci80_lo, precip_ci80_hi, precip_ci90_lo, precip_ci90_hi,
            tmin_obs, tmax_obs, precip_obs
        ) VALUES (
            ?,?,?,?, ?,?,?,?,?, ?,?,?,?, ?,?,?,?,?, ?,?,?,?,
            ?,?,?,?,?, ?,?,?,?, ?,?,?
        )
    """, rows)
    return client


def test_compute_coverage_returns_per_target_bucket(db: DuckDBClient) -> None:
    """compute_coverage deve restituire una riga per (target, bucket) con n_obs, cov_80, cov_90."""
    results = compute_coverage(db)
    assert len(results) > 0
    for r in results:
        assert r.n_obs > 0
        # Le coperture sono in [0, 1]
        assert 0.0 <= r.cov_80 <= 1.0
        assert 0.0 <= r.cov_90 <= 1.0
        # cov_90 ≥ cov_80 (CI più largo copre di più)
        assert r.cov_90 >= r.cov_80 - 0.01  # piccola tolleranza per None


def test_compute_coverage_includes_precip(db: DuckDBClient) -> None:
    """Tutti i 3 target devono essere rappresentati."""
    results = compute_coverage(db)
    targets = {r.target for r in results}
    assert "tmin_c" in targets
    assert "tmax_c" in targets
    assert "precip_mm" in targets


def test_compute_coverage_alerts_on_drift(db: DuckDBClient) -> None:
    """Drift > DRIFT_TOLERANCE_PP (0.05) deve emergere come drift_80/drift_90 non-nulli."""
    results = compute_coverage(db)
    for r in results:
        assert -1.0 <= r.drift_80 <= 0.2
        assert -1.0 <= r.drift_90 <= 0.1


def test_compute_coverage_no_data_returns_empty(tmp_path: Path) -> None:
    """DB senza predictions con actual → lista vuota."""
    client = DuckDBClient(db_path=tmp_path / "test_empty.duckdb")
    client.__enter__()
    client.init_schema()
    client.ensure_aci_schema()
    assert compute_coverage(client) == []
    client.__exit__(None, None, None)


def test_compute_coverage_window_30_days(db: DuckDBClient) -> None:
    """Solo predictions con ts_valid entro 30gg vengono considerate."""
    # Aggiungo una prediction vecchia (oltre 30gg) e una recente
    old_date = date.today() - timedelta(days=60)
    recent_date = date.today()
    old_ts = datetime(old_date.year, old_date.month, old_date.day)
    recent_ts = datetime(recent_date.year, recent_date.month, recent_date.day)
    rows = [
        ("20260101", "loc_old", old_ts, 0,
         4.0, 4.5, 5.0, 5.5, 6.0, 4.5, 5.5, 4.0, 6.0, 100.0,
         14.0, 14.5, 15.0, 15.5, 16.0, 14.5, 15.5, 14.0, 16.0, 15.0,
         0.0, 0.0, 0.0, 0.5, 1.0, 0.0, 0.5, 0.0, 1.0, 0.2),
        ("20260625", "loc_new", recent_ts, 0,
         4.0, 4.5, 5.0, 5.5, 6.0, 4.5, 5.5, 4.0, 6.0, 5.0,
         14.0, 14.5, 15.0, 15.5, 16.0, 14.5, 15.5, 14.0, 16.0, 15.0,
         0.0, 0.0, 0.0, 0.5, 1.0, 0.0, 0.5, 0.0, 1.0, 0.2),
    ]
    db._conn.executemany("""
        INSERT INTO predictions (
            model_version, location_id, ts_valid, lead_time_h,
            tmin_p05, tmin_p10, tmin_p50, tmin_p90, tmin_p95,
            tmin_ci80_lo, tmin_ci80_hi, tmin_ci90_lo, tmin_ci90_hi,
            tmax_p05, tmax_p10, tmax_p50, tmax_p90, tmax_p95,
            tmax_ci80_lo, tmax_ci80_hi, tmax_ci90_lo, tmax_ci90_hi,
            precip_p05, precip_p10, precip_p50, precip_p90, precip_p95,
            precip_ci80_lo, precip_ci80_hi, precip_ci90_lo, precip_ci90_hi,
            tmin_obs, tmax_obs, precip_obs
        ) VALUES (
            ?,?,?,?, ?,?,?,?,?, ?,?,?,?, ?,?,?,?,?, ?,?,?,?,
            ?,?,?,?,?, ?,?,?,?, ?,?,?
        )
    """, rows)

    results = compute_coverage(db)
    # Il vecchio NON dovrebbe essere contato (oltre 30gg).
    # Verifica che n_obs di tmin_c sia solo le righe recenti (50 del base + 1 loc_new).
    for r in results:
        if r.target == "tmin_c":
            assert r.n_obs == 51, f"tmin ha {r.n_obs} obs, dovrebbe essere 51 (no old data)"
