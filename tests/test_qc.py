"""Test per qc.py — quality control osservazioni SIR e Netatmo."""

from __future__ import annotations

from datetime import datetime, timedelta

from guazza.qc import (
    PRECIP_HIGH_MM,
    SPIKE_REALTIME_C,
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
    temp_c: float | None = None,
    location_id: str = "",
) -> None:
    db.execute(
        """
        INSERT INTO observations
            (source, station_id, location_id, ts, granularity,
             tmin_c, tmax_c, precip_mm, temp_c)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [source, station_id, location_id, ts, granularity, tmin_c, tmax_c, precip_mm, temp_c],
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


# ── spike_realtime ────────────────────────────────────────────────────────────

def test_spike_realtime_flags(db: DuckDBClient) -> None:
    """Due righe realtime distanti 30 min con |Δ| > SPIKE_REALTIME_C → flag."""
    t0 = datetime(2024, 6, 1, 10, 0)
    _insert_obs(db, "RT1", t0,                 temp_c=20.0, granularity="realtime")
    _insert_obs(db, "RT1", t0 + timedelta(minutes=30), temp_c=20.0 + SPIKE_REALTIME_C + 1, granularity="realtime")
    result = compute_quality_flags(db)
    assert result.get("spike_realtime", 0) >= 1


def test_spike_realtime_gap_no_flag(db: DuckDBClient) -> None:
    """Gap > 90 min → NON è uno spike (buco dati, non salto)."""
    t0 = datetime(2024, 6, 1, 10, 0)
    _insert_obs(db, "RT1", t0,                      temp_c=20.0, granularity="realtime")
    _insert_obs(db, "RT1", t0 + timedelta(minutes=120), temp_c=20.0 + SPIKE_REALTIME_C + 1, granularity="realtime")
    result = compute_quality_flags(db)
    assert result.get("spike_realtime", 0) == 0


def test_spike_realtime_small_delta_no_flag(db: DuckDBClient) -> None:
    """Δ = SPIKE_REALTIME_C esatto → NON flag (soglia strettamente maggiore)."""
    t0 = datetime(2024, 6, 1, 10, 0)
    _insert_obs(db, "RT1", t0,                       temp_c=20.0, granularity="realtime")
    _insert_obs(db, "RT1", t0 + timedelta(minutes=30), temp_c=20.0 + SPIKE_REALTIME_C, granularity="realtime")
    result = compute_quality_flags(db)
    assert result.get("spike_realtime", 0) == 0


def test_spike_realtime_netatmo(db: DuckDBClient) -> None:
    """spike_realtime funziona anche su source='netatmo'."""
    t0 = datetime(2024, 6, 1, 10, 0)
    _insert_obs(db, "NAT1", t0,                       temp_c=22.0, granularity="realtime", source="netatmo")
    _insert_obs(db, "NAT1", t0 + timedelta(minutes=30), temp_c=22.0 + SPIKE_REALTIME_C + 1, granularity="realtime", source="netatmo")
    result = compute_quality_flags(db)
    assert result.get("spike_realtime", 0) >= 1


def test_spike_realtime_daily_not_flagged(db: DuckDBClient) -> None:
    """Righe daily non vengono flaggate da spike_realtime."""
    t0 = datetime(2024, 6, 1)
    _insert_obs(db, "STA1", t0,                   tmin_c=10.0, tmax_c=20.0)
    _insert_obs(db, "STA1", t0 + timedelta(days=1), tmin_c=10.0, tmax_c=20.0)
    # Aggiungi anche righe realtime con spike per assicurarci che il flag daily sia 0
    _insert_obs(db, "RT1", t0,                       temp_c=20.0, granularity="realtime")
    _insert_obs(db, "RT1", t0 + timedelta(minutes=30), temp_c=20.0 + SPIKE_REALTIME_C + 1, granularity="realtime")
    compute_quality_flags(db)
    # Nessun daily flaggato da spike_realtime
    daily_rt_flags = db.execute(
        "SELECT COUNT(*) FROM quality_flags WHERE flag_type='spike_realtime' AND granularity='daily'"
    ).fetchone()[0]
    assert daily_rt_flags == 0


# ── stall_sensor ──────────────────────────────────────────────────────────────

def test_stall_7_rows_30min_flags(db: DuckDBClient) -> None:
    """7 righe realtime a 30 min con stesso temp_c (run 3h) → flag stall_sensor."""
    t0 = datetime(2024, 6, 1, 8, 0)
    for i in range(7):
        _insert_obs(db, "RT2", t0 + timedelta(minutes=30 * i), temp_c=15.0, granularity="realtime")
    result = compute_quality_flags(db)
    assert result.get("stall_sensor", 0) >= 1


def test_stall_gap_breaks_run(db: DuckDBClient) -> None:
    """Run rotta da gap > 90 min → nessun flag stall."""
    t0 = datetime(2024, 6, 1, 8, 0)
    # Prima metà della run (3 righe a 30 min)
    for i in range(3):
        _insert_obs(db, "RT3", t0 + timedelta(minutes=30 * i), temp_c=15.0, granularity="realtime")
    # Gap di 120 min — rompe la run
    t1 = t0 + timedelta(minutes=60 + 120)
    for i in range(4):
        _insert_obs(db, "RT3", t1 + timedelta(minutes=30 * i), temp_c=15.0, granularity="realtime")
    result = compute_quality_flags(db)
    assert result.get("stall_sensor", 0) == 0


def test_stall_changing_values_no_flag(db: DuckDBClient) -> None:
    """Valori variabili → nessun flag stall."""
    t0 = datetime(2024, 6, 1, 8, 0)
    for i, v in enumerate([15.0, 15.1, 15.2, 15.3, 15.4, 15.5, 15.6]):
        _insert_obs(db, "RT4", t0 + timedelta(minutes=30 * i), temp_c=v, granularity="realtime")
    result = compute_quality_flags(db)
    assert result.get("stall_sensor", 0) == 0


# ── bias_solar ────────────────────────────────────────────────────────────────

def _insert_forecast_wc(
    db: DuckDBClient,
    location_id: str,
    ts_run: datetime,
    ts_valid: datetime,
    weather_code: int,
) -> None:
    lead = int((ts_valid - ts_run).total_seconds() / 3600)
    for src in ("open_meteo_ecmwf_ifs", "open_meteo_icon_eu", "open_meteo_arome_france"):
        db.execute(
            """
            INSERT INTO forecasts
                (source, location_id, ts_run, ts_valid, lead_time_h, temp_c, weather_code)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [src, location_id, ts_run, ts_valid, lead, 20.0, weather_code],
        )


def test_bias_solar_flags_netatmo_clear_sky(db: DuckDBClient) -> None:
    """Netatmo realtime alle 14:00 ora locale con cielo sereno (wc=0) → flag bias_solar.

    In inverno UTC+1: ts_valid = 13:00 UTC per ora locale 14:00.
    """
    local_hour = 14  # ora locale Europe/Rome (UTC+1 in inverno)
    ts_utc = datetime(2025, 1, 15, local_hour - 1, 0)  # 13:00 UTC = 14:00 locale
    ts_run = datetime(2025, 1, 15, 0, 0)

    # Forecasts con cielo sereno per questa ora
    _insert_forecast_wc(db, "loc_solar", ts_run, ts_utc, weather_code=0)

    # Osservazione Netatmo realtime alla stessa ora
    _insert_obs(
        db, "NAT_S", ts_utc, temp_c=28.0,
        granularity="realtime", source="netatmo", location_id="loc_solar",
    )

    result = compute_quality_flags(db)
    assert result.get("bias_solar", 0) >= 1


def test_bias_solar_no_flag_outside_hours(db: DuckDBClient) -> None:
    """Ora locale 08:00 (fuori finestra 10-17) → nessun flag bias_solar."""
    local_hour = 8
    ts_utc = datetime(2025, 1, 15, local_hour - 1, 0)  # 07:00 UTC = 08:00 locale
    ts_run = datetime(2025, 1, 15, 0, 0)

    _insert_forecast_wc(db, "loc_solar", ts_run, ts_utc, weather_code=0)
    _insert_obs(
        db, "NAT_S", ts_utc, temp_c=28.0,
        granularity="realtime", source="netatmo", location_id="loc_solar",
    )

    result = compute_quality_flags(db)
    assert result.get("bias_solar", 0) == 0


def test_bias_solar_no_flag_cloudy(db: DuckDBClient) -> None:
    """weather_code 61 (pioggia) → NON flag bias_solar."""
    local_hour = 14
    ts_utc = datetime(2025, 1, 15, local_hour - 1, 0)
    ts_run = datetime(2025, 1, 15, 0, 0)

    _insert_forecast_wc(db, "loc_solar", ts_run, ts_utc, weather_code=61)
    _insert_obs(
        db, "NAT_S", ts_utc, temp_c=28.0,
        granularity="realtime", source="netatmo", location_id="loc_solar",
    )

    result = compute_quality_flags(db)
    assert result.get("bias_solar", 0) == 0


def test_bias_solar_no_flag_without_forecasts(db: DuckDBClient) -> None:
    """Senza forecasts → nessun flag bias_solar (conservative)."""
    ts_utc = datetime(2025, 1, 15, 13, 0)
    _insert_obs(
        db, "NAT_S", ts_utc, temp_c=28.0,
        granularity="realtime", source="netatmo", location_id="loc_solar",
    )
    result = compute_quality_flags(db)
    assert result.get("bias_solar", 0) == 0
