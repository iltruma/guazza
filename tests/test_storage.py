"""Test unitari per storage.py (DuckDB client wide)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from guazza.storage import DuckDBClient, open_db


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "test_guazza.duckdb"


def test_init_schema(tmp_db: Path) -> None:
    with DuckDBClient(db_path=tmp_db) as db:
        db.init_schema()
        ok = db.verify_schema()
    assert ok


def test_init_schema_idempotent(tmp_db: Path) -> None:
    with DuckDBClient(db_path=tmp_db) as db:
        db.init_schema()
        db.init_schema()
        ok = db.verify_schema()
    assert ok


def test_verify_schema_empty_db(tmp_db: Path) -> None:
    with DuckDBClient(db_path=tmp_db) as db:
        ok = db.verify_schema()
    assert not ok


def test_open_db_context_manager(tmp_db: Path) -> None:
    with open_db(db_path=tmp_db) as db:
        db.init_schema()
        result = db.execute("SELECT COUNT(*) FROM locations").fetchone()
    assert result is not None
    assert result[0] == 0


def test_execute_insert_select(tmp_db: Path) -> None:
    with DuckDBClient(db_path=tmp_db) as db:
        db.init_schema()
        db.execute(
            """
            INSERT INTO locations (id, label, lat, lon, elevation_m)
            VALUES (?, ?, ?, ?, ?)
            """,
            ["casa_campi", "Casa - Campi Bisenzio", 43.825, 11.140, 35],
        )
        result = db.execute("SELECT id, label FROM locations").fetchall()
    assert len(result) == 1
    assert result[0][0] == "casa_campi"
    assert result[0][1] == "Casa - Campi Bisenzio"


def test_no_connection_outside_context(tmp_db: Path) -> None:
    client = DuckDBClient(db_path=tmp_db)
    with pytest.raises(RuntimeError, match="context manager"):
        client.execute("SELECT 1")


def test_observations_wide_insert(tmp_db: Path) -> None:
    """Inserimento wide: una sola riga per (source, station_id, ts, granularity)."""
    with DuckDBClient(db_path=tmp_db) as db:
        db.init_schema()
        db.execute(
            """
            INSERT INTO observations
                (source, station_id, location_id, ts, granularity,
                 temp_c, humidity_pct, precip_mm)
            VALUES ('sir_toscana', 'TOS01001215', 'casa_campi',
                    '2024-06-15 00:00:00', 'daily', 28.5, 65.0, 0.0)
            """,
        )
        row = db.execute(
            "SELECT temp_c, humidity_pct, precip_mm FROM observations"
        ).fetchone()
    assert row == (28.5, 65.0, 0.0)


# ═════════════════════════════════════════════════════════════════════════════
# upsert_sir_observations — smoke test multi-sensore
# ═════════════════════════════════════════════════════════════════════════════

_STATION = "TOS01001215"
_LOCATION = "lavoro_cosimo"
_TS = datetime(2024, 6, 15)


def _termo_record() -> dict:
    return {
        "source": "sir_toscana",
        "station_id": _STATION,
        "location_id": _LOCATION,
        "ts": _TS,
        "granularity": "daily",
        "tmax_c": 31.0,
        "tmin_c": 14.5,
    }


def _pluvio_record() -> dict:
    return {
        "source": "sir_toscana",
        "station_id": _STATION,
        "location_id": _LOCATION,
        "ts": _TS,
        "granularity": "daily",
        "precip_mm": 3.2,
    }


def _igro_record() -> dict:
    return {
        "source": "sir_toscana",
        "station_id": _STATION,
        "location_id": _LOCATION,
        "ts": _TS,
        "granularity": "daily",
        "hum_med_pct": 72.0,
        "hum_min_pct": 45.0,
        "hum_max_pct": 95.0,
    }


def _anemo_record() -> dict:
    return {
        "source": "sir_toscana",
        "station_id": _STATION,
        "location_id": _LOCATION,
        "ts": _TS,
        "granularity": "daily",
        "wind_speed_ms": 2.1,
        "wind_dir_deg": 45.0,
        "wind_gust_ms": 8.5,
    }


def test_upsert_single_sensor_inserts_row(tmp_db: Path) -> None:
    """Un singolo sensore crea la riga."""
    with DuckDBClient(db_path=tmp_db) as db:
        db.init_schema()
        n = db.upsert_sir_observations([_termo_record()])
        count = db.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    assert n == 1
    assert count == 1


def test_upsert_multisensor_single_row(tmp_db: Path) -> None:
    """4 sensori per la stessa (station, ts) → 1 sola riga wide."""
    with DuckDBClient(db_path=tmp_db) as db:
        db.init_schema()
        db.upsert_sir_observations([_termo_record()])
        db.upsert_sir_observations([_pluvio_record()])
        db.upsert_sir_observations([_igro_record()])
        db.upsert_sir_observations([_anemo_record()])

        count = db.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        row = db.execute(
            """
            SELECT tmax_c, tmin_c, precip_mm, humidity_pct,
                   wind_speed_ms, wind_dir_deg, wind_gust_ms
            FROM observations
            WHERE station_id = ? AND ts = ?
            """,
            [_STATION, _TS],
        ).fetchone()

    assert count == 1, f"Attesa 1 riga, trovate {count}"
    tmax_c, tmin_c, precip_mm, humidity_pct, wind_speed_ms, wind_dir_deg, wind_gust_ms = row
    assert tmax_c == pytest.approx(31.0)
    assert tmin_c == pytest.approx(14.5)
    assert precip_mm == pytest.approx(3.2)
    assert humidity_pct == pytest.approx(72.0)
    assert wind_speed_ms == pytest.approx(2.1)
    assert wind_dir_deg == pytest.approx(45.0)
    assert wind_gust_ms == pytest.approx(8.5)


def test_upsert_does_not_overwrite_existing_values(tmp_db: Path) -> None:
    """COALESCE: un secondo upsert con NULL non sovrascrive valori già presenti."""
    with DuckDBClient(db_path=tmp_db) as db:
        db.init_schema()
        db.upsert_sir_observations([_termo_record()])
        # Secondo upsert: stesso (source, station, ts) ma tmax_c mancante
        partial = {
            "source": "sir_toscana",
            "station_id": _STATION,
            "location_id": _LOCATION,
            "ts": _TS,
            "granularity": "daily",
            "precip_mm": 5.0,
            # tmax_c assente → non deve cancellare il 31.0 precedente
        }
        db.upsert_sir_observations([partial])
        row = db.execute(
            "SELECT tmax_c, precip_mm FROM observations WHERE station_id = ? AND ts = ?",
            [_STATION, _TS],
        ).fetchone()

    assert row[0] == pytest.approx(31.0), "tmax_c deve essere preservato"
    assert row[1] == pytest.approx(5.0), "precip_mm deve essere aggiornato"


def test_upsert_multisensor_two_different_days(tmp_db: Path) -> None:
    """Stessa stazione, giorni diversi → 2 righe separate."""
    ts2 = datetime(2024, 6, 16)
    rec2 = {**_termo_record(), "ts": ts2, "tmax_c": 33.0, "tmin_c": 16.0}
    with DuckDBClient(db_path=tmp_db) as db:
        db.init_schema()
        db.upsert_sir_observations([_termo_record()])
        db.upsert_sir_observations([rec2])
        count = db.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    assert count == 2


def test_upsert_empty_list_returns_zero(tmp_db: Path) -> None:
    with DuckDBClient(db_path=tmp_db) as db:
        db.init_schema()
        n = db.upsert_sir_observations([])
    assert n == 0


def test_upsert_idempotent(tmp_db: Path) -> None:
    """Stesso record inserito due volte → 1 riga."""
    with DuckDBClient(db_path=tmp_db) as db:
        db.init_schema()
        db.upsert_sir_observations([_termo_record()])
        db.upsert_sir_observations([_termo_record()])
        count = db.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    assert count == 1


def test_upsert_igro_zero_humidity_preserved(tmp_db: Path) -> None:
    """hum_med_pct=0.0 non deve essere trattato come falsy — regressione bug 'or'."""
    rec = {
        "source": "sir_toscana",
        "station_id": _STATION,
        "location_id": _LOCATION,
        "ts": _TS,
        "granularity": "daily",
        "hum_med_pct": 0.0,
    }
    with DuckDBClient(db_path=tmp_db) as db:
        db.init_schema()
        db.upsert_sir_observations([rec])
        row = db.execute(
            "SELECT humidity_pct FROM observations WHERE station_id = ? AND ts = ?",
            [_STATION, _TS],
        ).fetchone()
    assert row is not None
    assert row[0] == pytest.approx(0.0), "humidity_pct=0.0 deve essere salvato, non ignorato"


def test_upsert_daily_and_realtime_same_ts_no_conflict(tmp_db: Path) -> None:
    """daily e realtime con stesso (source, station_id, ts) devono coesistere senza sovrascriversi."""
    daily = {**_termo_record(), "ts": _TS}  # granularity="daily", ts=00:00
    realtime = {
        "source": "sir_toscana",
        "station_id": _STATION,
        "location_id": _LOCATION,
        "ts": _TS,           # stesso ts esatto — edge case mezzanotte
        "granularity": "realtime",
        "temp_c": 12.5,
    }
    with DuckDBClient(db_path=tmp_db) as db:
        db.init_schema()
        db.upsert_sir_observations([daily])
        db.upsert_sir_observations([realtime])
        count = db.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        rows = db.execute(
            "SELECT granularity, tmax_c, temp_c FROM observations ORDER BY granularity"
        ).fetchall()
    assert count == 2, "daily e realtime devono essere righe distinte"
    gran = {r[0] for r in rows}
    assert gran == {"daily", "realtime"}
    daily_row = next(r for r in rows if r[0] == "daily")
    rt_row = next(r for r in rows if r[0] == "realtime")
    assert daily_row[1] == pytest.approx(31.0)   # tmax_c preservato
    assert rt_row[2] == pytest.approx(12.5)       # temp_c preservato


def test_upsert_sir_observations_dedup_within_batch(tmp_db: Path) -> None:
    """Batch con duplicati PK interni non deve crashare (DuckDB FatalException).

    Simula il caso backfill: termo_csv e pluvio0_24 producono entrambi
    una riga (sir_toscana, TOS01000891, 2022-01-01, daily).
    Il merge COALESCE deve preservare entrambi i valori.
    """
    ts = datetime(2022, 1, 1)
    records = [
        # primo sensore: termo_csv → ha tmax_c/tmin_c, no precip
        {
            "source": "sir_toscana", "station_id": "TOS01000891",
            "location_id": "casa_campi", "ts": ts, "granularity": "daily",
            "tmax_c": 10.0, "tmin_c": 2.0, "precip_mm": None,
        },
        # secondo sensore: pluvio0_24 → ha precip, no tmax/tmin
        {
            "source": "sir_toscana", "station_id": "TOS01000891",
            "location_id": "casa_campi", "ts": ts, "granularity": "daily",
            "tmax_c": None, "tmin_c": None, "precip_mm": 5.4,
        },
    ]
    with DuckDBClient(db_path=tmp_db) as db:
        db.init_schema()
        n = db.upsert_sir_observations(records)  # non deve crashare
        rows = db.execute(
            "SELECT tmax_c, tmin_c, precip_mm FROM observations WHERE station_id='TOS01000891'"
        ).fetchall()

    assert n == 2  # batch aveva 2 record
    assert len(rows) == 1  # ma nel DB una sola riga (deduplicata)
    assert rows[0][0] == pytest.approx(10.0)   # tmax_c dal primo sensore
    assert rows[0][1] == pytest.approx(2.0)    # tmin_c dal primo sensore
    assert rows[0][2] == pytest.approx(5.4)    # precip_mm dal secondo sensore


# ── upsert_forecasts — weather_code ──────────────────────────────────────────

def test_upsert_forecasts_weather_code_round_trip(tmp_db: Path) -> None:
    """weather_code viene scritto come INTEGER e riletto correttamente."""
    ts_run   = datetime(2026, 5, 18, 0, 0, 0)
    ts_valid = datetime(2026, 5, 18, 12, 0, 0)
    rec = {
        "source":      "open_meteo_ecmwf_ifs",
        "location_id": "casa_campi",
        "ts_run":      ts_run,
        "ts_valid":    ts_valid,
        "lead_time_h": 12,
        "temp_c":      18.5,
        "weather_code": 61,
    }
    with DuckDBClient(db_path=tmp_db) as db:
        db.init_schema()
        db.upsert_forecasts([rec])
        row = db.execute(
            "SELECT weather_code FROM forecasts WHERE source = ? AND ts_valid = ?",
            ["open_meteo_ecmwf_ifs", ts_valid],
        ).fetchone()

    assert row is not None
    assert row[0] == 61
    assert isinstance(row[0], int)


def test_upsert_forecasts_weather_code_replace(tmp_db: Path) -> None:
    """INSERT OR REPLACE aggiorna weather_code quando il run viene reinserito."""
    ts_run   = datetime(2026, 5, 18, 0, 0, 0)
    ts_valid = datetime(2026, 5, 18, 12, 0, 0)
    base = {
        "source": "open_meteo_icon_eu", "location_id": "casa_campi",
        "ts_run": ts_run, "ts_valid": ts_valid, "lead_time_h": 12,
        "temp_c": 20.0,
    }
    with DuckDBClient(db_path=tmp_db) as db:
        db.init_schema()
        db.upsert_forecasts([{**base, "weather_code": 3}])
        db.upsert_forecasts([{**base, "weather_code": 61}])
        row = db.execute(
            "SELECT weather_code FROM forecasts WHERE source = ? AND ts_valid = ?",
            ["open_meteo_icon_eu", ts_valid],
        ).fetchone()

    assert row is not None
    assert row[0] == 61


def test_upsert_forecasts_weather_code_null(tmp_db: Path) -> None:
    """weather_code NULL accettato senza errori."""
    ts_run   = datetime(2026, 5, 18, 6, 0, 0)
    ts_valid = datetime(2026, 5, 18, 18, 0, 0)
    rec = {
        "source": "open_meteo_gfs025", "location_id": "casa_campi",
        "ts_run": ts_run, "ts_valid": ts_valid, "lead_time_h": 12,
        "temp_c": 15.0,
        # weather_code assente → None
    }
    with DuckDBClient(db_path=tmp_db) as db:
        db.init_schema()
        db.upsert_forecasts([rec])
        row = db.execute(
            "SELECT weather_code FROM forecasts WHERE source = ?",
            ["open_meteo_gfs025"],
        ).fetchone()

    assert row is not None
    assert row[0] is None


def test_ensure_forecast_columns_idempotent(tmp_db: Path) -> None:
    """_ensure_forecast_columns chiamata più volte non solleva eccezioni."""
    with DuckDBClient(db_path=tmp_db) as db:
        db.init_schema()
        db._ensure_forecast_columns()   # seconda chiamata — deve essere no-op
        col_info = db.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = 'forecasts' AND column_name = 'weather_code'"
        ).fetchall()

    assert len(col_info) == 1
    assert col_info[0][1].upper() == "INTEGER"
