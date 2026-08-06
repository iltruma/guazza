"""Test per qc.py — quality control osservazioni SIR."""

from __future__ import annotations

from datetime import datetime, timedelta

from guazza.qc import (
    PRECIP_HIGH_MM,
    SPIKE_TEMP_C,
    compute_quality_flags,
)
from guazza.storage import DuckDBClient


def _insert_obs(
    db: DuckDBClient,
    station_id: str,
    ts: datetime,
    *,
    tmin_c: float | None = None,
    tmax_c: float | None = None,
    precip_mm: float | None = None,
    granularity: str = "daily",
    source: str = "sir_toscana",
) -> None:
    db.execute(
        """
        INSERT INTO observations
            (source, station_id, location_id, ts, granularity,
             tmin_c, tmax_c, precip_mm)
        VALUES (?, ?, '', ?, ?, ?, ?, ?)
        """,
        [source, station_id, ts, granularity, tmin_c, tmax_c, precip_mm],
    )


def test_no_flags_clean_data(db: DuckDBClient) -> None:
    t0 = datetime(2024, 1, 1)
    for i in range(5):
        _insert_obs(db, "STA1", t0 + timedelta(days=i), tmin_c=5.0 + i, tmax_c=15.0 + i, precip_mm=1.0)
    result = compute_quality_flags(db)
    assert result["total"] == 0


def test_spike_tmin(db: DuckDBClient) -> None:
    t0 = datetime(2024, 1, 1)
    _insert_obs(db, "STA1", t0, tmin_c=10.0, tmax_c=20.0)
    _insert_obs(db, "STA1", t0 + timedelta(days=1), tmin_c=10.0 + SPIKE_TEMP_C + 1, tmax_c=25.0)
    result = compute_quality_flags(db)
    assert result["total"] >= 1
    assert result.get("spike_tmin", 0) >= 1


def test_spike_tmax(db: DuckDBClient) -> None:
    t0 = datetime(2024, 1, 1)
    _insert_obs(db, "STA1", t0, tmin_c=5.0, tmax_c=20.0)
    _insert_obs(db, "STA1", t0 + timedelta(days=1), tmin_c=5.0, tmax_c=20.0 + SPIKE_TEMP_C + 1)
    result = compute_quality_flags(db)
    assert result["total"] >= 1
    assert result.get("spike_tmax", 0) >= 1


def test_inversion_temp(db: DuckDBClient) -> None:
    t0 = datetime(2024, 1, 1)
    _insert_obs(db, "STA1", t0, tmin_c=20.0, tmax_c=10.0)
    result = compute_quality_flags(db)
    assert result["total"] >= 1
    assert result.get("inversion_temp", 0) >= 1


def test_range_precip_high(db: DuckDBClient) -> None:
    t0 = datetime(2024, 1, 1)
    _insert_obs(db, "STA1", t0, tmin_c=5.0, tmax_c=15.0, precip_mm=PRECIP_HIGH_MM + 1)
    result = compute_quality_flags(db)
    assert result["total"] >= 1
    assert result.get("range_precip_high", 0) >= 1


def test_range_precip_high_realtime(db: DuckDBClient) -> None:
    t0 = datetime(2024, 1, 1)
    _insert_obs(db, "STA1", t0, precip_mm=PRECIP_HIGH_MM + 1, granularity="realtime")
    result = compute_quality_flags(db)
    assert result.get("range_precip_high", 0) >= 1


def test_below_spike_threshold_no_flag(db: DuckDBClient) -> None:
    t0 = datetime(2024, 1, 1)
    _insert_obs(db, "STA1", t0, tmin_c=10.0, tmax_c=20.0)
    _insert_obs(db, "STA1", t0 + timedelta(days=1), tmin_c=10.0 + SPIKE_TEMP_C, tmax_c=25.0)
    result = compute_quality_flags(db)
    assert result["total"] == 0


def test_spike_isolated_per_station(db: DuckDBClient) -> None:
    t0 = datetime(2024, 1, 1)
    _insert_obs(db, "STA1", t0, tmin_c=10.0, tmax_c=20.0)
    _insert_obs(db, "STA1", t0 + timedelta(days=1), tmin_c=10.0 + SPIKE_TEMP_C + 1, tmax_c=25.0)
    _insert_obs(db, "STA2", t0, tmin_c=10.0, tmax_c=20.0)
    _insert_obs(db, "STA2", t0 + timedelta(days=1), tmin_c=11.0, tmax_c=21.0)
    compute_quality_flags(db)
    sta2_flags = db.execute(
        "SELECT COUNT(*) FROM quality_flags WHERE station_id = 'STA2'"
    ).fetchone()[0]
    assert sta2_flags == 0


def test_idempotent(db: DuckDBClient) -> None:
    t0 = datetime(2024, 1, 1)
    _insert_obs(db, "STA1", t0, tmin_c=20.0, tmax_c=10.0)
    r1 = compute_quality_flags(db)
    r2 = compute_quality_flags(db)
    assert r1 == r2


def test_breakdown_keys(db: DuckDBClient) -> None:
    t0 = datetime(2024, 1, 1)
    _insert_obs(db, "STA1", t0, tmin_c=20.0, tmax_c=10.0)
    result = compute_quality_flags(db)
    assert "total" in result
    assert "inversion_temp" in result
    assert result["total"] == result["inversion_temp"]
