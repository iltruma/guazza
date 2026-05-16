"""Test per qc.py — quality control osservazioni SIR."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from guazza.qc import PRECIP_HIGH_MM, SPIKE_TEMP_C, compute_quality_flags
from guazza.storage import DuckDBClient


@pytest.fixture
def db(tmp_path: Path) -> DuckDBClient:
    client = DuckDBClient(db_path=tmp_path / "test.duckdb")
    client.__enter__()
    client.init_schema()
    return client


def _insert_obs(
    db: DuckDBClient,
    station_id: str,
    ts: datetime,
    *,
    tmin_c: float | None = None,
    tmax_c: float | None = None,
    precip_mm: float | None = None,
) -> None:
    db.execute(
        """
        INSERT INTO observations
            (source, station_id, location_id, ts, granularity, tmin_c, tmax_c, precip_mm)
        VALUES ('sir_toscana', ?, '', ?, 'daily', ?, ?, ?)
        """,
        [station_id, ts, tmin_c, tmax_c, precip_mm],
    )


def test_no_flags_clean_data(db: DuckDBClient) -> None:
    t0 = datetime(2024, 1, 1)
    for i in range(5):
        _insert_obs(db, "STA1", t0 + timedelta(days=i), tmin_c=5.0 + i, tmax_c=15.0 + i, precip_mm=1.0)
    n = compute_quality_flags(db)
    assert n == 0
    db.__exit__(None, None, None)


def test_spike_tmin(db: DuckDBClient) -> None:
    t0 = datetime(2024, 1, 1)
    _insert_obs(db, "STA1", t0, tmin_c=10.0, tmax_c=20.0)
    _insert_obs(db, "STA1", t0 + timedelta(days=1), tmin_c=10.0 + SPIKE_TEMP_C + 1, tmax_c=25.0)
    n = compute_quality_flags(db)
    assert n >= 1
    flags = db.execute("SELECT flag_type FROM quality_flags").fetchall()
    assert any(r[0] == "spike_tmin" for r in flags)
    db.__exit__(None, None, None)


def test_spike_tmax(db: DuckDBClient) -> None:
    t0 = datetime(2024, 1, 1)
    _insert_obs(db, "STA1", t0, tmin_c=5.0, tmax_c=20.0)
    _insert_obs(db, "STA1", t0 + timedelta(days=1), tmin_c=5.0, tmax_c=20.0 + SPIKE_TEMP_C + 1)
    n = compute_quality_flags(db)
    assert n >= 1
    flags = db.execute("SELECT flag_type FROM quality_flags").fetchall()
    assert any(r[0] == "spike_tmax" for r in flags)
    db.__exit__(None, None, None)


def test_inversion_temp(db: DuckDBClient) -> None:
    t0 = datetime(2024, 1, 1)
    _insert_obs(db, "STA1", t0, tmin_c=20.0, tmax_c=10.0)
    n = compute_quality_flags(db)
    assert n >= 1
    flags = db.execute("SELECT flag_type FROM quality_flags").fetchall()
    assert any(r[0] == "inversion_temp" for r in flags)
    db.__exit__(None, None, None)


def test_range_precip_high(db: DuckDBClient) -> None:
    t0 = datetime(2024, 1, 1)
    _insert_obs(db, "STA1", t0, tmin_c=5.0, tmax_c=15.0, precip_mm=PRECIP_HIGH_MM + 1)
    n = compute_quality_flags(db)
    assert n >= 1
    flags = db.execute("SELECT flag_type FROM quality_flags").fetchall()
    assert any(r[0] == "range_precip_high" for r in flags)
    db.__exit__(None, None, None)


def test_below_spike_threshold_no_flag(db: DuckDBClient) -> None:
    t0 = datetime(2024, 1, 1)
    _insert_obs(db, "STA1", t0, tmin_c=10.0, tmax_c=20.0)
    _insert_obs(db, "STA1", t0 + timedelta(days=1), tmin_c=10.0 + SPIKE_TEMP_C, tmax_c=25.0)
    n = compute_quality_flags(db)
    assert n == 0
    db.__exit__(None, None, None)


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
    db.__exit__(None, None, None)


def test_idempotent(db: DuckDBClient) -> None:
    t0 = datetime(2024, 1, 1)
    _insert_obs(db, "STA1", t0, tmin_c=20.0, tmax_c=10.0)
    n1 = compute_quality_flags(db)
    n2 = compute_quality_flags(db)
    assert n1 == n2
    db.__exit__(None, None, None)
