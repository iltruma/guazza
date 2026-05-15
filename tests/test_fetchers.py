"""Test unitari per fetchers.py (SIR + Netatmo + Open-Meteo wide + ARPAT)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from guazza.fetchers import (
    _extract_measures,
    _infer_ts_run,
    _measure_ts,
    _parse_om_response,
    _qc_range,
    _StationData,
    fetch_netatmo_location,
    fetch_openmeteo_forecast,
    fetch_openmeteo_historical,
    fetch_sir_historical,
    fetch_sir_realtime,
    save_netatmo_to_db,
)
from guazza.storage import DuckDBClient

# ═════════════════════════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════════════════════════

_HEADER_BLOCK = """\
"Stazione";"Test"
"Codice";"TOS00000001"
"Comune";"Firenze"
"Provincia";"FI"
"GB [m]";"E";0;"N";0
"WGS84 [°]";"Lat";0.000;"Lon";0.000
"Quota [m]";0,00

;;;ATTENZIONE
;;;separatore punto e virgola
;;;decimale virgola
;;;N = non validato
;;;R = ricostruito
;;;I = incerto
;;;@ = mancante
;;;V = validato
;;;P = prevalidato

"""


def _make_csv(header_cols: str, data_rows: list[str]) -> str:
    lines = _HEADER_BLOCK.splitlines()
    lines.append(f'"gg/mm/aaaa";{header_cols}')
    lines.extend(data_rows)
    return "\n".join(lines)


def _mock_response(csv_text: str) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.text = csv_text
    resp.raise_for_status = MagicMock()
    return resp


def _patched_sir_fetch(station_id: str, sensor_type: str, csv_text: str, location_id: str = "") -> list:
    mock_resp = _mock_response(csv_text)
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.get = MagicMock(return_value=mock_resp)
    with patch("guazza.fetchers.httpx.Client", return_value=mock_client):
        return fetch_sir_historical(station_id, sensor_type, location_id)


# ═════════════════════════════════════════════════════════════════════════════
# SIR Historical
# ═════════════════════════════════════════════════════════════════════════════


def test_termo_csv_wide() -> None:
    """termo_csv: una riga per giorno con tmax e tmin."""
    csv_text = _make_csv(
        '"Max [°C]";"Min [°C]"',
        ["15/06/2024;28,5;14,2", "16/06/2024;31,0;16,8"],
    )
    rows = _patched_sir_fetch("TOS01001215", "termo_csv", csv_text, "lavoro_cosimo")

    assert len(rows) == 2
    assert rows[0]["tmax_c"] == pytest.approx(28.5)
    assert rows[0]["tmin_c"] == pytest.approx(14.2)
    assert rows[0]["ts"] == datetime(2024, 6, 15)
    assert rows[0]["source"] == "sir_toscana"
    assert rows[0]["station_id"] == "TOS01001215"
    assert rows[0]["location_id"] == "lavoro_cosimo"

    assert rows[1]["tmax_c"] == pytest.approx(31.0)
    assert rows[1]["tmin_c"] == pytest.approx(16.8)


def test_termo_csv_empty_cells() -> None:
    csv_text = _make_csv('"Max [°C]";"Min [°C]"', ["01/01/1992;;"])
    rows = _patched_sir_fetch("TOS01001215", "termo_csv", csv_text)
    assert len(rows) == 1
    assert rows[0]["tmax_c"] is None
    assert rows[0]["tmin_c"] is None


def test_pluvio_flag_mapping() -> None:
    csv_text = _make_csv(
        '"Precipitazione [mm]";"Tipo Dato"',
        ["01/01/2024;5,2;V", "02/01/2024;0,0;N", "03/01/2024;1,0;P",
         "04/01/2024;3,0;R", "05/01/2024;0,5;I", "06/01/2024;@;@"],
    )
    rows = _patched_sir_fetch("TOS01001215", "pluvio0_24", csv_text)
    assert len(rows) == 6
    assert rows[0]["precip_mm"] == pytest.approx(5.2)
    assert rows[5]["precip_mm"] is None


def test_igro_three_variables() -> None:
    csv_text = _make_csv(
        '"Med [%]";"Min [%]";"Max [%]"',
        ["12/04/2022;72,0;45,0;95,0"],
    )
    rows = _patched_sir_fetch("TOS01001215", "igro0_24", csv_text)
    assert len(rows) == 1
    assert rows[0]["hum_med_pct"] == pytest.approx(72.0)
    assert rows[0]["hum_min_pct"] == pytest.approx(45.0)
    assert rows[0]["hum_max_pct"] == pytest.approx(95.0)


def test_anemo_column_order() -> None:
    csv_text = _make_csv(
        '"Vel Med [m/s]";"Dir Med";"Vel Max [m/s]"',
        ["01/01/2024;1,5;NE;6,2"],
    )
    rows = _patched_sir_fetch("TOS01001215", "anemo0_24", csv_text)
    assert len(rows) == 1
    assert rows[0]["wind_speed_ms"] == pytest.approx(1.5)
    assert rows[0]["wind_gust_ms"] == pytest.approx(6.2)
    assert rows[0]["wind_dir_deg"] == pytest.approx(45.0)


def test_idro_flag_col() -> None:
    csv_text = _make_csv(
        '"Livello [m]";"Tipo Dato"',
        ["01/03/2023;1,23;V", "02/03/2023;1,45;R"],
    )
    rows = _patched_sir_fetch("TOS01004591", "idro_l", csv_text)
    assert len(rows) == 2
    assert rows[0]["level_m"] == pytest.approx(1.23)
    assert rows[1]["level_m"] == pytest.approx(1.45)


def test_unsupported_sensor_type() -> None:
    with pytest.raises(ValueError, match="non supportato"):
        fetch_sir_historical("TOS01001215", "termo")


def test_empty_response() -> None:
    csv_text = _make_csv('"Max [°C]";"Min [°C]"', [])
    rows = _patched_sir_fetch("TOS01001215", "termo_csv", csv_text)
    assert rows == []


def test_invalid_date_row_skipped() -> None:
    csv_text = _make_csv(
        '"Max [°C]";"Min [°C]"',
        ["NOT_A_DATE;25,0;12,0", "15/06/2024;28,0;14,0"],
    )
    rows = _patched_sir_fetch("TOS01001215", "termo_csv", csv_text)
    assert len(rows) == 1
    assert rows[0]["ts"] == datetime(2024, 6, 15)


# ═════════════════════════════════════════════════════════════════════════════
# SIR Realtime
# ═════════════════════════════════════════════════════════════════════════════


def test_fetch_sir_realtime_parses_json() -> None:
    # Nuova struttura endpoint /monitoraggio/actions.php
    mock_data = {
        "termo":  {"date": "15/05/2026 12:15:00", "value": "22.5", "id": "TOS01001215"},
        "igro":   {"date": "15/05/2026 12:15:00", "value": "65",   "id": "TOS01001215"},
        "anemo":  {"date": "15/05/2026 12:15:00", "speed": "1.5",  "dir": "45.0",
                   "speed_label": None, "id": "TOS01001215"},
        "pluvio": {"date": "15/05/2026 12:00:00", "CUM00": "0.2",  "CUM01": "0.0",
                   "CUM24": "3.6", "id": "TOS01001215"},
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json = MagicMock(return_value=mock_data)
    mock_resp.raise_for_status = MagicMock()
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.get = MagicMock(return_value=mock_resp)

    with patch("guazza.fetchers.httpx.Client", return_value=mock_client):
        record = fetch_sir_realtime("TOS01001215")

    assert record["source"] == "sir_toscana"
    assert record["station_id"] == "TOS01001215"
    assert record["temp_c"] == pytest.approx(22.5)
    assert record["humidity_pct"] == pytest.approx(65.0)
    assert record["wind_speed_ms"] == pytest.approx(1.5)
    assert record["wind_dir_deg"] == pytest.approx(45.0)   # dir già in gradi, no lookup
    assert record["precip_mm"] == pytest.approx(0.0)
    assert "wind_gust_ms" not in record                     # non esposta dal nuovo endpoint


def test_fetch_sir_realtime_dash_precip() -> None:
    """CUM01 = '-' (dato non disponibile) → precip_mm assente nel record."""
    mock_data = {
        "termo":  {"value": "10.0"},
        "pluvio": {"CUM01": "-", "CUM24": "-"},
    }
    mock_resp = MagicMock()
    mock_resp.json = MagicMock(return_value=mock_data)
    mock_resp.raise_for_status = MagicMock()
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.get = MagicMock(return_value=mock_resp)

    with patch("guazza.fetchers.httpx.Client", return_value=mock_client):
        record = fetch_sir_realtime("TOS01001215")

    assert "precip_mm" not in record


# ═════════════════════════════════════════════════════════════════════════════
# Netatmo
# ═════════════════════════════════════════════════════════════════════════════

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
            }
        },
    },
    {
        "_id": "70:ee:50:ff:00:11",
        "place": {"location": [11.15, 43.82], "altitude": 42},
        "measures": {
            "02:00:00:ff:00:11": {
                "type": ["temperature", "humidity"],
                "res": {str(_TS_UNIX): [38.0, 30.0]},
            }
        },
    },
]

_LOC = {"lat": 43.82, "lon": 11.13, "elevation_m": 42}


def test_extract_measures() -> None:
    measures = {
        "02:aa": {"type": ["temperature", "humidity"], "res": {"123": [21.5, 65.0]}},
    }
    m = _extract_measures(measures)
    assert m["temp_c"] == 21.5
    assert m["humidity_pct"] == 65.0
    assert m["rain_1h"] is None


def test_measure_ts() -> None:
    measures = {"02:aa": {"type": ["temperature"], "res": {str(_TS_UNIX): [18.0]}}}
    ts = _measure_ts(measures)
    assert ts == datetime.fromtimestamp(_TS_UNIX, tz=UTC)


def test_qc_range_valid() -> None:
    assert _qc_range(20.0, 65.0) is True


def test_qc_range_temp_high() -> None:
    assert _qc_range(60.0, 65.0) is False


def test_fetch_netatmo_location_returns_stations() -> None:
    env = {"access_token": "fake", "refresh_token": "", "client_id": "", "client_secret": ""}
    with patch("guazza.fetchers._fetch_public_data", return_value=_MOCK_STATIONS):
        stations = fetch_netatmo_location("casa_campi", _LOC, env)
    assert len(stations) == 3


def test_fetch_netatmo_location_outlier_flagged() -> None:
    env = {"access_token": "fake", "refresh_token": "", "client_id": "", "client_secret": ""}
    with patch("guazza.fetchers._fetch_public_data", return_value=_MOCK_STATIONS):
        stations = fetch_netatmo_location("casa_campi", _LOC, env)
    outlier = next(sd for sd in stations if sd.mac == "70:ee:50:ff:00:11")
    assert outlier.qc_cross is False
    assert outlier.qc_pass is False


def test_fetch_netatmo_location_valid_pass() -> None:
    env = {"access_token": "fake", "refresh_token": "", "client_id": "", "client_secret": ""}
    with patch("guazza.fetchers._fetch_public_data", return_value=_MOCK_STATIONS):
        stations = fetch_netatmo_location("casa_campi", _LOC, env)
    valid = [sd for sd in stations if sd.mac != "70:ee:50:ff:00:11"]
    assert all(sd.qc_pass for sd in valid)


@pytest.fixture
def seeded_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "test.duckdb"
    with DuckDBClient(db_path=db_path) as db:
        db.init_schema()
    return db_path


def _make_station(mac: str, temp: float, qc: bool = True) -> _StationData:
    return _StationData(
        mac=mac,
        lat=43.82,
        lon=11.13,
        alt_m=40,
        distance_km=0.5,
        delta_elev_m=2.0,
        weight=0.35,
        measures={"temp_c": temp, "humidity_pct": 65.0, "rain_1h": None, "wind_speed_ms": None},
        ts=datetime(2026, 5, 14, 10, 0, 0, tzinfo=UTC),
        qc_range=qc,
        qc_cross=qc,
    )


def test_save_netatmo_to_db_inserts_fetch_log(seeded_db: Path) -> None:
    stations = [_make_station("70:ee:50:aa:bb:cc", 18.5)]
    fetched_at = datetime(2026, 5, 14, 10, 0, 0, tzinfo=UTC)
    with DuckDBClient(db_path=seeded_db) as db:
        save_netatmo_to_db(db, "casa_campi", stations, fetched_at)
        count = db.execute("SELECT COUNT(*) FROM netatmo_fetch_log").fetchone()[0]
    assert count == 1


def test_save_netatmo_to_db_inserts_observations_wide(seeded_db: Path) -> None:
    stations = [_make_station("70:ee:50:aa:bb:cc", 18.5)]
    fetched_at = datetime(2026, 5, 14, 10, 0, 0, tzinfo=UTC)
    with DuckDBClient(db_path=seeded_db) as db:
        save_netatmo_to_db(db, "casa_campi", stations, fetched_at)
        count = db.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        assert count == 1
        row = db.execute("SELECT temp_c, humidity_pct FROM observations").fetchone()
    assert row[0] == pytest.approx(18.5)
    assert row[1] == pytest.approx(65.0)


def test_save_netatmo_to_db_idempotent(seeded_db: Path) -> None:
    stations = [_make_station("70:ee:50:aa:bb:cc", 18.5)]
    fetched_at = datetime(2026, 5, 14, 10, 0, 0, tzinfo=UTC)
    with DuckDBClient(db_path=seeded_db) as db:
        save_netatmo_to_db(db, "casa_campi", stations, fetched_at)
        save_netatmo_to_db(db, "casa_campi", stations, fetched_at)
        count_log = db.execute("SELECT COUNT(*) FROM netatmo_fetch_log").fetchone()[0]
        count_obs = db.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    assert count_log == 1
    assert count_obs == 1


# ═════════════════════════════════════════════════════════════════════════════
# Open-Meteo
# ═════════════════════════════════════════════════════════════════════════════

# Risposta mock minimale Open-Meteo (2 ore, un modello)
_OM_NOW = datetime(2026, 5, 15, 7, 30, 0, tzinfo=UTC)  # 07:30 UTC
_OM_TS_RUN_ECMWF = datetime(2026, 5, 15, 0, 0, 0, tzinfo=UTC)   # ECMWF run 00 UTC
_OM_TS_RUN_ICON = datetime(2026, 5, 15, 6, 0, 0, tzinfo=UTC)    # ICON-EU run 06 UTC

_OM_MOCK_RESPONSE = {
    "latitude": 43.76,
    "longitude": 11.19,
    "timezone": "UTC",
    "hourly": {
        "time": ["2026-05-15T08:00", "2026-05-15T09:00"],
        "temperature_2m": [18.5, 19.2],
        "relative_humidity_2m": [72.0, 68.0],
        "precipitation": [0.0, 0.2],
        "wind_speed_10m": [2.1, 3.4],
        "wind_direction_10m": [180.0, 190.0],
        "wind_gusts_10m": [5.0, 6.5],
        "surface_pressure": [1013.2, 1012.8],
    },
}


def test_infer_ts_run_ecmwf_before_noon() -> None:
    """07:30 UTC → ECMWF run 00 UTC (ultimo run ≤ 07:30 tra [0, 12])."""
    now = datetime(2026, 5, 15, 7, 30, tzinfo=UTC)
    ts_run = _infer_ts_run("ecmwf_ifs025", now)
    assert ts_run == datetime(2026, 5, 15, 0, 0, tzinfo=UTC)


def test_infer_ts_run_ecmwf_after_noon() -> None:
    """14:00 UTC → ECMWF run 12 UTC."""
    now = datetime(2026, 5, 15, 14, 0, tzinfo=UTC)
    ts_run = _infer_ts_run("ecmwf_ifs025", now)
    assert ts_run == datetime(2026, 5, 15, 12, 0, tzinfo=UTC)


def test_infer_ts_run_icon_eu_mid() -> None:
    """07:30 UTC → ICON-EU run 06 UTC (ogni 3h: 0,3,6,9,...)."""
    now = datetime(2026, 5, 15, 7, 30, tzinfo=UTC)
    ts_run = _infer_ts_run("icon_eu", now)
    assert ts_run == datetime(2026, 5, 15, 6, 0, tzinfo=UTC)


def test_infer_ts_run_midnight_edge() -> None:
    """00:30 UTC → ECMWF run 00 UTC (non il run del giorno precedente)."""
    now = datetime(2026, 5, 15, 0, 30, tzinfo=UTC)
    ts_run = _infer_ts_run("ecmwf_ifs025", now)
    assert ts_run == datetime(2026, 5, 15, 0, 0, tzinfo=UTC)


def test_infer_ts_run_before_first_run() -> None:
    """00:00 UTC esatto → ECMWF run 00 UTC (il run delle 00 è disponibile)."""
    now = datetime(2026, 5, 15, 0, 0, tzinfo=UTC)
    ts_run = _infer_ts_run("ecmwf_ifs025", now)
    assert ts_run == datetime(2026, 5, 15, 0, 0, tzinfo=UTC)


def test_parse_om_response_record_count() -> None:
    """_parse_om_response: 2 ore → 2 record."""
    records = _parse_om_response(
        _OM_MOCK_RESPONSE, "ecmwf_ifs025", "lavoro_cosimo", _OM_TS_RUN_ECMWF
    )
    assert len(records) == 2


def test_parse_om_response_fields() -> None:
    """Verifica mapping variabili Open-Meteo → colonne wide."""
    records = _parse_om_response(
        _OM_MOCK_RESPONSE, "ecmwf_ifs025", "lavoro_cosimo", _OM_TS_RUN_ECMWF
    )
    r = records[0]
    assert r["source"] == "open_meteo_ecmwf_ifs025"
    assert r["location_id"] == "lavoro_cosimo"
    assert r["ts_run"] == _OM_TS_RUN_ECMWF
    assert r["ts_valid"] == datetime(2026, 5, 15, 8, 0, tzinfo=UTC)
    assert r["lead_time_h"] == 8   # 08:00 - 00:00 = 8h
    assert r["temp_c"] == pytest.approx(18.5)
    assert r["humidity_pct"] == pytest.approx(72.0)
    assert r["precip_mm"] == pytest.approx(0.0)
    assert r["wind_speed_ms"] == pytest.approx(2.1)
    assert r["wind_dir_deg"] == pytest.approx(180.0)
    assert r["wind_gust_ms"] == pytest.approx(5.0)
    assert r["pressure_hpa"] == pytest.approx(1013.2)


def test_parse_om_response_lead_time_increases() -> None:
    """lead_time_h cresce di 1 tra record consecutivi (dati orari)."""
    records = _parse_om_response(
        _OM_MOCK_RESPONSE, "ecmwf_ifs025", "lavoro_cosimo", _OM_TS_RUN_ECMWF
    )
    assert records[1]["lead_time_h"] == records[0]["lead_time_h"] + 1


def test_parse_om_response_empty_data() -> None:
    """Risposta senza hourly → lista vuota."""
    records = _parse_om_response({}, "ecmwf_ifs025", "lavoro_cosimo", _OM_TS_RUN_ECMWF)
    assert records == []


def test_parse_om_response_null_values() -> None:
    """Valori None nell'array → colonna None nel record."""
    data = {
        "hourly": {
            "time": ["2026-05-15T08:00"],
            "temperature_2m": [None],
            "relative_humidity_2m": [None],
            "precipitation": [None],
            "wind_speed_10m": [None],
            "wind_direction_10m": [None],
            "wind_gusts_10m": [None],
            "surface_pressure": [None],
        }
    }
    records = _parse_om_response(data, "ecmwf_ifs025", "lavoro_cosimo", _OM_TS_RUN_ECMWF)
    assert len(records) == 1
    assert records[0]["temp_c"] is None
    assert records[0]["precip_mm"] is None


def test_fetch_openmeteo_forecast_calls_api() -> None:
    """fetch_openmeteo_forecast chiama _fetch_om_json e ritorna record per ogni modello."""
    with patch("guazza.fetchers._fetch_om_json", return_value=_OM_MOCK_RESPONSE):
        results = fetch_openmeteo_forecast(
            location_id="lavoro_cosimo",
            lat=43.76,
            lon=11.19,
            models=["ecmwf_ifs025"],
            now_utc=_OM_NOW,
        )
    assert "ecmwf_ifs025" in results
    assert len(results["ecmwf_ifs025"]) == 2


def test_fetch_openmeteo_forecast_error_returns_empty() -> None:
    """Se il fetch fallisce → lista vuota per quel modello, nessuna eccezione."""
    with patch("guazza.fetchers._fetch_om_json", side_effect=Exception("timeout")):
        results = fetch_openmeteo_forecast(
            location_id="lavoro_cosimo",
            lat=43.76,
            lon=11.19,
            models=["ecmwf_ifs025"],
            now_utc=_OM_NOW,
        )
    assert results["ecmwf_ifs025"] == []


@pytest.fixture
def seeded_db_forecasts(tmp_path: Path) -> Path:
    db_path = tmp_path / "test_forecasts.duckdb"
    with DuckDBClient(db_path=db_path) as db:
        db.init_schema()
    return db_path


def test_upsert_forecasts_inserts(seeded_db_forecasts: Path) -> None:
    """upsert_forecasts: inserisce i record nella tabella forecasts."""
    records = _parse_om_response(
        _OM_MOCK_RESPONSE, "ecmwf_ifs025", "lavoro_cosimo", _OM_TS_RUN_ECMWF
    )
    with DuckDBClient(db_path=seeded_db_forecasts) as db:
        n = db.upsert_forecasts(records)
        count = db.execute("SELECT COUNT(*) FROM forecasts").fetchone()[0]
    assert n == 2
    assert count == 2


def test_upsert_forecasts_idempotent(seeded_db_forecasts: Path) -> None:
    """Stesso batch inserito due volte → stessa riga, no duplicati."""
    records = _parse_om_response(
        _OM_MOCK_RESPONSE, "ecmwf_ifs025", "lavoro_cosimo", _OM_TS_RUN_ECMWF
    )
    with DuckDBClient(db_path=seeded_db_forecasts) as db:
        db.upsert_forecasts(records)
        db.upsert_forecasts(records)
        count = db.execute("SELECT COUNT(*) FROM forecasts").fetchone()[0]
    assert count == 2


def test_upsert_forecasts_update_on_conflict(seeded_db_forecasts: Path) -> None:
    """Stesso (source, location, ts_run, ts_valid) con temp diversa → vince il secondo."""
    rec_v1 = _parse_om_response(
        _OM_MOCK_RESPONSE, "ecmwf_ifs025", "lavoro_cosimo", _OM_TS_RUN_ECMWF
    )
    # Modifica la temperatura nel secondo batch
    import copy
    data_v2 = copy.deepcopy(_OM_MOCK_RESPONSE)
    data_v2["hourly"]["temperature_2m"] = [99.9, 99.9]
    rec_v2 = _parse_om_response(data_v2, "ecmwf_ifs025", "lavoro_cosimo", _OM_TS_RUN_ECMWF)

    with DuckDBClient(db_path=seeded_db_forecasts) as db:
        db.upsert_forecasts(rec_v1)
        db.upsert_forecasts(rec_v2)
        row = db.execute(
            "SELECT temp_c FROM forecasts ORDER BY ts_valid LIMIT 1"
        ).fetchone()
    assert row[0] == pytest.approx(99.9)


def test_upsert_forecasts_empty(seeded_db_forecasts: Path) -> None:
    with DuckDBClient(db_path=seeded_db_forecasts) as db:
        n = db.upsert_forecasts([])
    assert n == 0


# ═════════════════════════════════════════════════════════════════════════════
# Open-Meteo — modalità storica (ts_run=None)
# ═════════════════════════════════════════════════════════════════════════════

# Risposta mock storica: 3 ore consecutive per testare ts_run variabile
_OM_HISTORICAL_MOCK = {
    "latitude": 43.76,
    "longitude": 11.19,
    "timezone": "UTC",
    "hourly": {
        # ECMWF run 00: ts_valid 00:00-11:00 → ts_run=00:00
        # ECMWF run 12: ts_valid 12:00-23:00 → ts_run=12:00
        "time": ["2026-05-14T11:00", "2026-05-14T12:00", "2026-05-14T13:00"],
        "temperature_2m": [20.0, 21.0, 21.5],
        "relative_humidity_2m": [50.0, 48.0, 46.0],
        "precipitation": [0.0, 0.0, 0.0],
        "wind_speed_10m": [3.0, 3.5, 4.0],
        "wind_direction_10m": [180.0, 185.0, 190.0],
        "wind_gusts_10m": [6.0, 7.0, 8.0],
        "surface_pressure": [1012.0, 1011.5, 1011.0],
    },
}


def test_parse_om_response_historical_ts_run_inferred() -> None:
    """ts_run=None → inferita per riga: 11:00 UTC → ts_run=00:00, 12:00 → ts_run=12:00."""
    records = _parse_om_response(_OM_HISTORICAL_MOCK, "ecmwf_ifs025", "lavoro_cosimo", ts_run=None)
    assert len(records) == 3

    # ts_valid 11:00 → ECMWF run 00 UTC → lead_time_h = 11
    r11 = records[0]
    assert r11["ts_run"] == datetime(2026, 5, 14, 0, 0, tzinfo=UTC)
    assert r11["lead_time_h"] == 11

    # ts_valid 12:00 → ECMWF run 12 UTC → lead_time_h = 0
    r12 = records[1]
    assert r12["ts_run"] == datetime(2026, 5, 14, 12, 0, tzinfo=UTC)
    assert r12["lead_time_h"] == 0

    # ts_valid 13:00 → ECMWF run 12 UTC → lead_time_h = 1
    r13 = records[2]
    assert r13["ts_run"] == datetime(2026, 5, 14, 12, 0, tzinfo=UTC)
    assert r13["lead_time_h"] == 1


def test_parse_om_response_historical_values() -> None:
    """Valori correttamente estratti in modalità storica."""
    records = _parse_om_response(_OM_HISTORICAL_MOCK, "ecmwf_ifs025", "lavoro_cosimo", ts_run=None)
    r = records[0]
    assert r["temp_c"] == pytest.approx(20.0)
    assert r["humidity_pct"] == pytest.approx(50.0)
    assert r["wind_speed_ms"] == pytest.approx(3.0)
    assert r["pressure_hpa"] == pytest.approx(1012.0)


def test_fetch_openmeteo_historical_uses_parse_om_response() -> None:
    """fetch_openmeteo_historical produce record con ts_run inferred correttamente."""
    with patch("guazza.fetchers._fetch_om_json", return_value=_OM_HISTORICAL_MOCK):
        results = fetch_openmeteo_historical(
            location_id="lavoro_cosimo",
            lat=43.76,
            lon=11.19,
            start_date="2026-05-14",
            end_date="2026-05-14",
            models=["ecmwf_ifs025"],
        )
    assert "ecmwf_ifs025" in results
    records = results["ecmwf_ifs025"]
    assert len(records) == 3
    # ts_run varia per riga (non fissa)
    assert records[0]["ts_run"] != records[1]["ts_run"]
    # lead_time_h corretto
    assert records[1]["lead_time_h"] == 0  # ts_valid=12:00, ts_run=12:00


def test_fetch_openmeteo_historical_error_returns_empty() -> None:
    """Se il fetch fallisce → lista vuota, nessuna eccezione."""
    with patch("guazza.fetchers._fetch_om_json", side_effect=Exception("timeout")):
        results = fetch_openmeteo_historical(
            location_id="lavoro_cosimo",
            lat=43.76,
            lon=11.19,
            start_date="2026-05-14",
            end_date="2026-05-14",
            models=["ecmwf_ifs025"],
        )
    assert results["ecmwf_ifs025"] == []


def test_upsert_forecasts_batch_historical(seeded_db_forecasts: Path) -> None:
    """upsert_forecasts batch: 3 record storici con ts_run diverse → 3 righe."""
    records = _parse_om_response(_OM_HISTORICAL_MOCK, "ecmwf_ifs025", "lavoro_cosimo", ts_run=None)
    with DuckDBClient(db_path=seeded_db_forecasts) as db:
        n = db.upsert_forecasts(records)
        count = db.execute("SELECT COUNT(*) FROM forecasts").fetchone()[0]
        # verifica lead_time_h corretto per la riga con ts_valid=12:00
        row = db.execute(
            "SELECT lead_time_h FROM forecasts WHERE ts_valid = '2026-05-14T12:00:00+00:00'"
        ).fetchone()
    assert n == 3
    assert count == 3
    # verifica lead_time_h corretto per la riga con ts_valid=12:00
    ts_12 = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
    with DuckDBClient(db_path=seeded_db_forecasts) as db:
        row = db.execute(
            "SELECT lead_time_h FROM forecasts WHERE ts_valid = ?", [ts_12]
        ).fetchone()
    assert row is not None
    assert row[0] == 0


# ═════════════════════════════════════════════════════════════════════════════
# precip_interval_h — SIR storico e SIR realtime
# ═════════════════════════════════════════════════════════════════════════════

def test_sir_historical_pluvio_has_precip_interval_24() -> None:
    """pluvio0_24 deve produrre precip_interval_h=24 e granularity='daily'."""
    csv_text = _make_csv('"precip [mm]";"flag"', ['15/06/2024;2,4;V', '16/06/2024;0,0;V'])
    rows = _patched_sir_fetch("TOS00000001", "pluvio0_24", csv_text)
    assert len(rows) == 2
    assert all(r.get("precip_interval_h") == 24 for r in rows)
    assert all(r.get("granularity") == "daily" for r in rows)


def test_sir_historical_termo_no_precip_interval() -> None:
    """termo_csv deve avere granularity='daily' e precip_interval_h=None."""
    csv_text = _make_csv('"Tmax [°C]";"Tmin [°C]"', ['15/06/2024;31,0;14,5'])
    rows = _patched_sir_fetch("TOS00000001", "termo_csv", csv_text)
    assert len(rows) == 1
    assert all(r.get("precip_interval_h") is None for r in rows)
    assert all(r.get("granularity") == "daily" for r in rows)


def test_sir_realtime_precip_interval_1() -> None:
    """CUM01 in SIR realtime deve produrre precip_interval_h=1 e granularity='realtime'."""
    from guazza.fetchers import fetch_sir_realtime
    mock_json = {
        "pluvio": {"CUM01": "3.4", "CUM24": "5.4"},
        "termo": {"value": "18.0", "date": "15/05/2026 10:30"},
    }
    mock_resp = MagicMock()
    mock_resp.json.return_value = mock_json
    mock_resp.raise_for_status = MagicMock()
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.get = MagicMock(return_value=mock_resp)
    with patch("guazza.fetchers.httpx.Client", return_value=mock_client):
        rec = fetch_sir_realtime("TOS99999999")
    assert rec["precip_mm"] == pytest.approx(3.4)
    assert rec["precip_interval_h"] == 1
    assert rec["granularity"] == "realtime"


# ═════════════════════════════════════════════════════════════════════════════
# _parse_sir_realtime_ts — timestamp da campo date
# ═════════════════════════════════════════════════════════════════════════════

def test_parse_sir_realtime_ts_from_termo() -> None:
    """Deve parsare la data dal campo termo.date."""
    from guazza.fetchers import _parse_sir_realtime_ts
    data = {"termo": {"value": "18.0", "date": "15/05/2026 10:30"}}
    ts = _parse_sir_realtime_ts(data)
    assert ts == datetime(2026, 5, 15, 10, 30, tzinfo=UTC)


def test_parse_sir_realtime_ts_fallback_now() -> None:
    """Senza campo date deve tornare un ts vicino a now(UTC)."""
    from guazza.fetchers import _parse_sir_realtime_ts
    before = datetime.now(tz=UTC)
    ts = _parse_sir_realtime_ts({})
    after = datetime.now(tz=UTC)
    assert before <= ts <= after


def test_parse_sir_realtime_ts_unparsable_fallback() -> None:
    """Se date non è parsabile deve tornare un ts vicino a now(UTC)."""
    from guazza.fetchers import _parse_sir_realtime_ts
    data = {"termo": {"value": "18.0", "date": "invalid-date"}}
    before = datetime.now(tz=UTC)
    ts = _parse_sir_realtime_ts(data)
    after = datetime.now(tz=UTC)
    assert before <= ts <= after


# ═════════════════════════════════════════════════════════════════════════════
# ARPAT — fetch_arpat_nrt + fetch_arpat_bollettini
# ═════════════════════════════════════════════════════════════════════════════

from guazza.fetchers import fetch_arpat_bollettini, fetch_arpat_nrt  # noqa: E402

_ARPAT_STATIONS_SINGLE = [{"id": "FI-SIGNA", "weight": 1.0}]
_ARPAT_STATIONS_MULTI = [
    {"id": "PO-ROMA", "weight": 0.7},
    {"id": "PO-FERRUCCI", "weight": 0.3},
]


def _patch_arpat_json(payload: Any) -> Any:
    """Mocker per _fetch_arpat_json — bypassa retry tenacity e httpx."""
    return patch("guazza.fetchers._fetch_arpat_json", return_value=payload)


def _patch_arpat_json_error(exc: Exception) -> Any:
    """Mocker per _fetch_arpat_json che solleva eccezione."""
    return patch("guazza.fetchers._fetch_arpat_json", side_effect=exc)


# ── NRT ──────────────────────────────────────────────────────────────────────

def test_arpat_nrt_single_station_list_format() -> None:
    """Formato lista: una stazione → un record con NO2 e O3."""
    payload = [
        {
            "codice_stazione": "FI-SIGNA",
            "misurazioni": {"NO2": "25.3", "O3": "48.7", "CO": "0.4"},
        }
    ]
    with _patch_arpat_json(payload):
        records = fetch_arpat_nrt("casa_campi", _ARPAT_STATIONS_SINGLE)

    assert len(records) == 1
    r = records[0]
    assert r["source"] == "arpat"
    assert r["station_id"] == "FI-SIGNA"
    assert r["location_id"] == "casa_campi"
    assert r["granularity"] == "hourly"
    assert r["no2_ugm3"] == pytest.approx(25.3)
    assert r["o3_ugm3"] == pytest.approx(48.7)
    assert r["weight"] == pytest.approx(1.0)


def test_arpat_nrt_dict_format_stazioni_key() -> None:
    """Formato dict con chiave 'stazioni'."""
    payload = {
        "stazioni": [
            {
                "codice_stazione": "FI-SIGNA",
                "misurazioni": {"NO2": "10.0", "O3": "60.0"},
            }
        ]
    }
    with _patch_arpat_json(payload):
        records = fetch_arpat_nrt("casa_campi", _ARPAT_STATIONS_SINGLE)

    assert len(records) == 1
    assert records[0]["no2_ugm3"] == pytest.approx(10.0)


def test_arpat_nrt_multi_station_weights() -> None:
    """Due stazioni con pesi diversi → due record."""
    payload = [
        {"codice_stazione": "PO-ROMA",     "misurazioni": {"NO2": "30.0"}},
        {"codice_stazione": "PO-FERRUCCI", "misurazioni": {"NO2": "45.0"}},
    ]
    with _patch_arpat_json(payload):
        records = fetch_arpat_nrt("lavoro_madda", _ARPAT_STATIONS_MULTI)

    assert len(records) == 2
    by_id = {r["station_id"]: r for r in records}
    assert by_id["PO-ROMA"]["weight"] == pytest.approx(0.7)
    assert by_id["PO-FERRUCCI"]["weight"] == pytest.approx(0.3)


def test_arpat_nrt_station_not_in_response() -> None:
    """Stazione nella config ma assente dalla risposta → record saltato."""
    payload = [
        {"codice_stazione": "ALTRA-STAZIONE", "misurazioni": {"NO2": "10.0"}},
    ]
    with _patch_arpat_json(payload):
        records = fetch_arpat_nrt("casa_campi", _ARPAT_STATIONS_SINGLE)

    assert records == []


def test_arpat_nrt_null_values() -> None:
    """Valori null/assenti → colonne None, non eccezione."""
    payload = [
        {"codice_stazione": "FI-SIGNA", "misurazioni": {"NO2": None, "O3": ""}},
    ]
    with _patch_arpat_json(payload):
        records = fetch_arpat_nrt("casa_campi", _ARPAT_STATIONS_SINGLE)

    assert len(records) == 1
    assert records[0]["no2_ugm3"] is None
    assert records[0]["o3_ugm3"] is None


def test_arpat_nrt_http_error_returns_empty() -> None:
    """Errore generico in _fetch_arpat_json → lista vuota, nessuna eccezione propagata."""
    with _patch_arpat_json_error(Exception("timeout")):
        records = fetch_arpat_nrt("casa_campi", _ARPAT_STATIONS_SINGLE)

    assert records == []


def test_arpat_nrt_ts_from_response() -> None:
    """Timestamp dalla risposta deve essere parsato correttamente."""
    payload = [
        {
            "codice_stazione": "FI-SIGNA",
            "data": "2026-05-15T10:00:00",
            "misurazioni": {"NO2": "20.0"},
        }
    ]
    with _patch_arpat_json(payload):
        records = fetch_arpat_nrt("casa_campi", _ARPAT_STATIONS_SINGLE)

    assert len(records) == 1
    assert records[0]["ts"] == datetime(2026, 5, 15, 10, 0, 0, tzinfo=UTC)


# ── Bollettini ────────────────────────────────────────────────────────────────

def test_arpat_bollettini_single_station() -> None:
    """Bollettino giornaliero → record con PM10 e PM2.5, granularity='daily'."""
    payload = [
        {
            "codice_stazione": "FI-SIGNA",
            "misurazioni": {"PM10": "35.0", "PM2.5": "18.0", "NO2": "22.0"},
        }
    ]
    with patch("guazza.fetchers._fetch_arpat_json") as mock_fetch:
        mock_fetch.return_value = payload
        records = fetch_arpat_bollettini("casa_campi", _ARPAT_STATIONS_SINGLE, date="2026-05-14")

    assert mock_fetch.call_count == 1
    call_kwargs = mock_fetch.call_args.kwargs
    assert call_kwargs["params"]["stazione"] == "FI-SIGNA"
    assert call_kwargs["params"]["startdate"] == "2026-05-14"
    assert call_kwargs["params"]["enddate"] == "2026-05-14"
    assert call_kwargs["params"]["limit"] == "1000"

    assert len(records) == 1
    r = records[0]
    assert r["source"] == "arpat"
    assert r["granularity"] == "daily"
    assert r["pm10_ugm3"] == pytest.approx(35.0)
    assert r["pm25_ugm3"] == pytest.approx(18.0)
    assert r["no2_ugm3"] == pytest.approx(22.0)
    assert r["ts"] == datetime(2026, 5, 14, tzinfo=UTC)


def test_arpat_bollettini_invalid_date_raises() -> None:
    """Data non valida → ValueError prima del fetch."""
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        fetch_arpat_bollettini("casa_campi", _ARPAT_STATIONS_SINGLE, date="14-05-2026")


def test_arpat_bollettini_http_error_returns_empty() -> None:
    """Errore generico in _fetch_arpat_json → lista vuota."""
    with _patch_arpat_json_error(Exception("conn refused")):
        records = fetch_arpat_bollettini("casa_campi", _ARPAT_STATIONS_SINGLE, date="2026-05-14")

    assert records == []


def test_arpat_bollettini_missing_pm_values_none() -> None:
    """Valori PM assenti nella risposta → None, non eccezione."""
    payload = [
        {"codice_stazione": "FI-SIGNA", "misurazioni": {}},
    ]
    with _patch_arpat_json(payload):
        records = fetch_arpat_bollettini("casa_campi", _ARPAT_STATIONS_SINGLE, date="2026-05-14")

    assert len(records) == 1
    assert records[0]["pm10_ugm3"] is None
    assert records[0]["pm25_ugm3"] is None


def test_arpat_bollettini_params_per_station() -> None:
    """_fetch_arpat_json chiamato con params corretti per ogni stazione."""
    stations = [
        {"id": "PO-ROMA", "weight": 0.7},
        {"id": "PO-FERRUCCI", "weight": 0.3},
    ]
    with patch("guazza.fetchers._fetch_arpat_json") as mock_fetch:
        mock_fetch.return_value = {"stazioni": [{"misurazioni": {"PM10": "10.0"}}]}
        records = fetch_arpat_bollettini("lavoro_madda", stations, date="2026-05-14")

    assert mock_fetch.call_count == 2
    calls = mock_fetch.call_args_list
    assert calls[0].kwargs["params"]["stazione"] == "PO-ROMA"
    assert calls[0].kwargs["params"]["startdate"] == "2026-05-14"
    assert calls[0].kwargs["params"]["enddate"] == "2026-05-14"
    assert calls[0].kwargs["params"]["limit"] == "1000"
    assert calls[1].kwargs["params"]["stazione"] == "PO-FERRUCCI"
    assert len(records) == 2
