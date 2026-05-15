"""Test unitari per netatmo_realtime.py.

L'API Netatmo non viene chiamata: _fetch_public_data è mockata via monkeypatch.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from guazza.ingestion.netatmo_realtime import (
    QC_CROSS_SIGMA,
    QC_SIR_SIGMA,
    _StationData,
    _apply_sir_qc,
    _extract_measures,
    _get_recent_sir_temp,
    _measure_ts,
    _qc_range,
    _weighted_mean,
    fetch_location,
    save_to_db,
)
from guazza.storage.duckdb_client import DuckDBClient


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def seeded_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "test.duckdb"
    with DuckDBClient(db_path=db_path) as db:
        db.init_schema()
        db.run_migrations()
    return db_path


# Risposta API fittizia: 2 stazioni vicine con dati validi + 1 outlier
_TS_UNIX = 1747220000
_MOCK_STATIONS = [
    {
        "_id": "70:ee:50:aa:bb:cc",
        "place": {"location": [11.13, 43.82], "altitude": 40},
        "measures": {
            "02:00:00:aa:bb:cc": {
                "type": ["temperature", "humidity"],
                "res": {str(_TS_UNIX): [18.5, 72.0]},
            }
        },
    },
    {
        "_id": "70:ee:50:dd:ee:ff",
        "place": {"location": [11.14, 43.83], "altitude": 45},
        "measures": {
            "02:00:00:dd:ee:ff": {
                "type": ["temperature", "humidity"],
                "res": {str(_TS_UNIX): [19.0, 68.0]},
            },
            "06:00:00:dd:ee:ff": {
                "type": ["wind_strength", "wind_angle"],
                "res": {str(_TS_UNIX): [12.0, 180.0]},
            },
        },
    },
    {
        # outlier: temperatura molto alta → qc_cross=False
        "_id": "70:ee:50:ff:00:11",
        "place": {"location": [11.15, 43.82], "altitude": 42},
        "measures": {
            "02:00:00:ff:00:11": {
                "type": ["temperature", "humidity"],
                "res": {str(_TS_UNIX): [38.0, 30.0]},  # 38°C — outlier
            }
        },
    },
]

_LOC = {
    "lat": 43.82,
    "lon": 11.13,
    "elevation_m": 42,
}


# ── Test _extract_measures ────────────────────────────────────────────────────


def test_extract_temperature_humidity() -> None:
    measures = {
        "02:aa": {
            "type": ["temperature", "humidity"],
            "res": {"123": [21.5, 65.0]},
        }
    }
    m = _extract_measures(measures)
    assert m["temperature_2m"] == 21.5
    assert m["humidity"] == 65.0
    assert m["rain_1h"] is None
    assert m["wind_speed"] is None


def test_extract_rain_sum_rain_1() -> None:
    measures = {
        "06:aa": {
            "type": ["sum_rain_1"],
            "res": {"123": [2.4]},
        }
    }
    m = _extract_measures(measures)
    assert m["rain_1h"] == 2.4


def test_extract_wind_strength() -> None:
    measures = {
        "06:aa": {
            "type": ["wind_strength", "wind_angle"],
            "res": {"123": [15.0, 90.0]},
        }
    }
    m = _extract_measures(measures)
    assert m["wind_speed"] == 15.0


def test_extract_multi_module() -> None:
    """Temperatura da NAModule1 + vento da NAModule2 — stessa stazione."""
    measures = {
        "02:aa": {
            "type": ["temperature", "humidity"],
            "res": {"123": [20.0, 70.0]},
        },
        "06:aa": {
            "type": ["wind_strength"],
            "res": {"123": [8.0]},
        },
    }
    m = _extract_measures(measures)
    assert m["temperature_2m"] == 20.0
    assert m["wind_speed"] == 8.0


def test_extract_empty_measures() -> None:
    m = _extract_measures({})
    assert all(v is None for v in m.values())


def test_measure_ts_extracts_unix() -> None:
    measures = {"02:aa": {"type": ["temperature"], "res": {str(_TS_UNIX): [18.0]}}}
    ts = _measure_ts(measures)
    assert ts == datetime.fromtimestamp(_TS_UNIX, tz=timezone.utc)


def test_measure_ts_fallback() -> None:
    """Measures vuoto → fallback a now (non crashare)."""
    ts = _measure_ts({})
    assert ts.tzinfo is not None


# ── Test QC ───────────────────────────────────────────────────────────────────


def test_qc_range_valid() -> None:
    assert _qc_range(20.0, 65.0) is True


def test_qc_range_temp_too_high() -> None:
    assert _qc_range(60.0, 65.0) is False


def test_qc_range_temp_too_low() -> None:
    assert _qc_range(-25.0, 65.0) is False


def test_qc_range_humidity_over_100() -> None:
    assert _qc_range(20.0, 105.0) is False


def test_qc_range_none_values() -> None:
    """None non scatena il range check."""
    assert _qc_range(None, None) is True


def test_weighted_mean_basic() -> None:
    mean = _weighted_mean([(10.0, 1.0), (20.0, 1.0)])
    assert mean == pytest.approx(15.0)


def test_weighted_mean_empty() -> None:
    assert _weighted_mean([]) is None


def test_weighted_mean_skewed() -> None:
    mean = _weighted_mean([(10.0, 3.0), (20.0, 1.0)])
    assert mean == pytest.approx(12.5)


# ── Test fetch_location (con mock API) ───────────────────────────────────────


def test_fetch_location_returns_stations() -> None:
    env = {"access_token": "fake_token", "refresh_token": "", "client_id": "", "client_secret": ""}
    with patch("guazza.ingestion.netatmo_realtime._fetch_public_data", return_value=_MOCK_STATIONS):
        stations = fetch_location("casa_campi", _LOC, env)
    assert len(stations) == 3


def test_fetch_location_outlier_flagged() -> None:
    """La stazione a 38°C deve avere qc_cross=False."""
    env = {"access_token": "fake_token", "refresh_token": "", "client_id": "", "client_secret": ""}
    with patch("guazza.ingestion.netatmo_realtime._fetch_public_data", return_value=_MOCK_STATIONS):
        stations = fetch_location("casa_campi", _LOC, env)
    outlier = next(sd for sd in stations if sd.mac == "70:ee:50:ff:00:11")
    assert outlier.qc_cross is False
    assert outlier.qc_pass is False


def test_fetch_location_valid_stations_pass_qc() -> None:
    """Le stazioni con T normale devono avere qc_pass=True."""
    env = {"access_token": "fake_token", "refresh_token": "", "client_id": "", "client_secret": ""}
    with patch("guazza.ingestion.netatmo_realtime._fetch_public_data", return_value=_MOCK_STATIONS):
        stations = fetch_location("casa_campi", _LOC, env)
    valid = [sd for sd in stations if sd.mac != "70:ee:50:ff:00:11"]
    assert all(sd.qc_pass for sd in valid)


def test_fetch_location_weights_positive() -> None:
    """Tutti i pesi devono essere > 0."""
    env = {"access_token": "fake_token", "refresh_token": "", "client_id": "", "client_secret": ""}
    with patch("guazza.ingestion.netatmo_realtime._fetch_public_data", return_value=_MOCK_STATIONS):
        stations = fetch_location("casa_campi", _LOC, env)
    assert all(sd.weight > 0 for sd in stations)


def test_fetch_location_station_with_no_location_skipped() -> None:
    """Stazioni senza coordinate (location=[None,None]) vengono saltate."""
    bad_station = {"_id": "70:ee:50:00:00:00", "place": {"location": [None, None]}, "measures": {}}
    env = {"access_token": "fake_token", "refresh_token": "", "client_id": "", "client_secret": ""}
    with patch("guazza.ingestion.netatmo_realtime._fetch_public_data", return_value=[bad_station]):
        stations = fetch_location("casa_campi", _LOC, env)
    assert len(stations) == 0


# ── Test save_to_db ───────────────────────────────────────────────────────────


def _make_station(mac: str, temp: float, qc: bool = True) -> _StationData:
    return _StationData(
        mac=mac,
        lat=43.82,
        lon=11.13,
        alt_m=40,
        distance_km=0.5,
        delta_elev_m=2.0,
        weight=0.35,
        measures={
            "temperature_2m": temp,
            "humidity": 65.0,
            "rain_1h": None,
            "wind_speed": None,
        },
        ts=datetime(2026, 5, 14, 10, 0, 0, tzinfo=timezone.utc),
        qc_range=qc,
        qc_cross=qc,
    )


def test_save_to_db_inserts_fetch_log(seeded_db: Path) -> None:
    stations = [_make_station("70:ee:50:aa:bb:cc", 18.5)]
    fetched_at = datetime(2026, 5, 14, 10, 0, 0, tzinfo=timezone.utc)
    with DuckDBClient(db_path=seeded_db) as db:
        save_to_db(db, "casa_campi", stations, fetched_at)
        count = db.execute("SELECT COUNT(*) FROM netatmo_fetch_log").fetchone()[0]
    assert count == 1


def test_save_to_db_inserts_observations(seeded_db: Path) -> None:
    """Temperatura + umidità = 2 righe in observations per stazione."""
    stations = [_make_station("70:ee:50:aa:bb:cc", 18.5)]
    fetched_at = datetime(2026, 5, 14, 10, 0, 0, tzinfo=timezone.utc)
    with DuckDBClient(db_path=seeded_db) as db:
        save_to_db(db, "casa_campi", stations, fetched_at)
        count = db.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    assert count == 2  # temperature_2m + humidity


def test_save_to_db_idempotent(seeded_db: Path) -> None:
    """Chiamare save_to_db due volte con stessi dati non duplica."""
    stations = [_make_station("70:ee:50:aa:bb:cc", 18.5)]
    fetched_at = datetime(2026, 5, 14, 10, 0, 0, tzinfo=timezone.utc)
    with DuckDBClient(db_path=seeded_db) as db:
        save_to_db(db, "casa_campi", stations, fetched_at)
        save_to_db(db, "casa_campi", stations, fetched_at)
        count_log = db.execute("SELECT COUNT(*) FROM netatmo_fetch_log").fetchone()[0]
        count_obs = db.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    assert count_log == 1
    assert count_obs == 2


def test_save_to_db_qc_pass_stored(seeded_db: Path) -> None:
    """qc_pass viene salvato correttamente in observations."""
    stations = [_make_station("70:ee:50:aa:bb:cc", 18.5, qc=False)]
    fetched_at = datetime(2026, 5, 14, 10, 0, 0, tzinfo=timezone.utc)
    with DuckDBClient(db_path=seeded_db) as db:
        save_to_db(db, "casa_campi", stations, fetched_at)
        rows = db.execute("SELECT qc_pass FROM observations").fetchall()
    assert all(row[0] is False for row in rows)


def test_save_to_db_empty_no_crash(seeded_db: Path) -> None:
    fetched_at = datetime(2026, 5, 14, 10, 0, 0, tzinfo=timezone.utc)
    with DuckDBClient(db_path=seeded_db) as db:
        save_to_db(db, "casa_campi", [], fetched_at)


# ── Test QC SIR ───────────────────────────────────────────────────────────────


def _make_station_simple(mac: str, temp: float) -> _StationData:
    return _StationData(
        mac=mac,
        lat=43.82,
        lon=11.13,
        alt_m=40,
        distance_km=0.5,
        delta_elev_m=2.0,
        weight=0.35,
        measures={
            "temperature_2m": temp,
            "humidity": 65.0,
            "rain_1h": None,
            "wind_speed": None,
        },
        ts=datetime(2026, 5, 14, 10, 0, 0, tzinfo=timezone.utc),
    )


def test_apply_sir_qc_within_threshold() -> None:
    """Stazione entro ±8°C dalla SIR → qc_sir=True."""
    stations = [_make_station_simple("aa", 20.0)]
    _apply_sir_qc(stations, sir_temp=18.0)
    assert stations[0].qc_sir is True


def test_apply_sir_qc_outside_threshold() -> None:
    """Stazione oltre 8°C dalla SIR → qc_sir=False."""
    stations = [_make_station_simple("aa", 30.0)]
    _apply_sir_qc(stations, sir_temp=18.0)
    assert stations[0].qc_sir is False


def test_apply_sir_qc_boundary() -> None:
    """Esattamente alla soglia (diff=8.0) → qc_sir=True (<=, non <)."""
    stations = [_make_station_simple("aa", 26.0)]
    _apply_sir_qc(stations, sir_temp=18.0)
    assert stations[0].qc_sir is True


def test_apply_sir_qc_no_temp() -> None:
    """Stazione senza temperatura → qc_sir non modificato (rimane True)."""
    sd = _make_station_simple("aa", 20.0)
    sd.measures["temperature_2m"] = None
    _apply_sir_qc([sd], sir_temp=18.0)
    assert sd.qc_sir is True


def test_apply_sir_qc_custom_threshold() -> None:
    """Threshold personalizzato."""
    stations = [_make_station_simple("aa", 22.0)]
    _apply_sir_qc(stations, sir_temp=18.0, threshold_c=3.0)
    assert stations[0].qc_sir is False


def test_qc_pass_false_when_sir_fails() -> None:
    """qc_pass deve essere False se qc_sir=False (anche con range/cross ok)."""
    sd = _make_station_simple("aa", 35.0)
    sd.qc_sir = False
    assert sd.qc_pass is False


def test_get_recent_sir_temp_no_data(seeded_db: Path) -> None:
    """DB senza osservazioni SIR → restituisce None."""
    with DuckDBClient(db_path=seeded_db) as db:
        result = _get_recent_sir_temp(db, "casa_campi")
    assert result is None


def test_get_recent_sir_temp_with_data(seeded_db: Path) -> None:
    """Con osservazione SIR recente → restituisce il valore."""
    with DuckDBClient(db_path=seeded_db) as db:
        db.execute(
            """
            INSERT INTO observations
                (source, station_id, location_id, ts, variable, value, flag, weight, qc_pass)
            VALUES ('sir', 'TOS01001225', 'casa_campi', CURRENT_TIMESTAMP,
                    'temperature_2m', 17.3, 'ok', 1.0, true)
            """
        )
        result = _get_recent_sir_temp(db, "casa_campi")
    assert result == pytest.approx(17.3)


def test_get_recent_sir_temp_too_old(seeded_db: Path) -> None:
    """Osservazione SIR più vecchia di 60 min → None."""
    with DuckDBClient(db_path=seeded_db) as db:
        db.execute(
            """
            INSERT INTO observations
                (source, station_id, location_id, ts, variable, value, flag, weight, qc_pass)
            VALUES ('sir', 'TOS01001225', 'casa_campi',
                    CURRENT_TIMESTAMP - INTERVAL '120 minutes',
                    'temperature_2m', 17.3, 'ok', 1.0, true)
            """
        )
        result = _get_recent_sir_temp(db, "casa_campi")
    assert result is None


def test_fetch_location_with_sir_qc(seeded_db: Path) -> None:
    """Con dati SIR nel DB, l'outlier a 38°C viene escluso anche da qc_sir."""
    env = {"access_token": "fake_token", "refresh_token": "", "client_id": "", "client_secret": ""}
    with DuckDBClient(db_path=seeded_db) as db:
        # Inserisci osservazione SIR recente a ~19°C
        db.execute(
            """
            INSERT INTO observations
                (source, station_id, location_id, ts, variable, value, flag, weight, qc_pass)
            VALUES ('sir', 'TOS01001225', 'casa_campi', CURRENT_TIMESTAMP,
                    'temperature_2m', 19.0, 'ok', 1.0, true)
            """
        )
        with patch("guazza.ingestion.netatmo_realtime._fetch_public_data", return_value=_MOCK_STATIONS):
            stations = fetch_location("casa_campi", _LOC, env, db=db)

    outlier = next(sd for sd in stations if sd.mac == "70:ee:50:ff:00:11")
    # 38°C - 19°C = 19°C > 8°C → qc_sir=False
    assert outlier.qc_sir is False
    assert outlier.qc_pass is False


def test_fetch_location_without_db_no_sir_qc() -> None:
    """Senza db, qc_sir rimane True (QC SIR saltato)."""
    env = {"access_token": "fake_token", "refresh_token": "", "client_id": "", "client_secret": ""}
    with patch("guazza.ingestion.netatmo_realtime._fetch_public_data", return_value=_MOCK_STATIONS):
        stations = fetch_location("casa_campi", _LOC, env, db=None)
    assert all(sd.qc_sir is True for sd in stations)
