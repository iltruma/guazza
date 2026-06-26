"""Test unitari per fetchers.py (SIR + Netatmo + Open-Meteo wide + ARPAT)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from guazza.fetch_netatmo import (
    _extract_measures,
    _measure_ts,
    _qc_range,
    _StationData,
    fetch_netatmo_location,
    save_netatmo_to_db,
)
from guazza.fetch_openmeteo import (
    _infer_ts_run,
    _multilead_hourly_params,
    _parse_om_multilead,
    _parse_om_response,
)
from guazza.fetch_sir import fetch_sir_historical, fetch_sir_realtime
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
    with patch("guazza.fetch_sir.httpx.Client", return_value=mock_client):
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

    with patch("guazza.fetch_sir.httpx.Client", return_value=mock_client):
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

    with patch("guazza.fetch_sir.httpx.Client", return_value=mock_client):
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
    # UTC naive (convenzione DB): stesso istante dell'epoch, tzinfo strippato
    assert ts.tzinfo is None
    assert ts == datetime.fromtimestamp(_TS_UNIX, tz=UTC).replace(tzinfo=None)


def test_qc_range_valid() -> None:
    assert _qc_range(20.0, 65.0) is True


def test_qc_range_temp_high() -> None:
    assert _qc_range(60.0, 65.0) is False


def test_fetch_netatmo_location_returns_stations() -> None:
    env = {"access_token": "fake", "refresh_token": "", "client_id": "", "client_secret": ""}
    with patch("guazza.fetch_netatmo._fetch_public_data", return_value=_MOCK_STATIONS):
        stations = fetch_netatmo_location("casa_campi", _LOC, env)
    assert len(stations) == 3


def test_fetch_netatmo_location_outlier_flagged() -> None:
    env = {"access_token": "fake", "refresh_token": "", "client_id": "", "client_secret": ""}
    with patch("guazza.fetch_netatmo._fetch_public_data", return_value=_MOCK_STATIONS):
        stations = fetch_netatmo_location("casa_campi", _LOC, env)
    outlier = next(sd for sd in stations if sd.mac == "70:ee:50:ff:00:11")
    assert outlier.qc_cross is False
    assert outlier.qc_pass is False


def test_fetch_netatmo_location_valid_pass() -> None:
    env = {"access_token": "fake", "refresh_token": "", "client_id": "", "client_secret": ""}
    with patch("guazza.fetch_netatmo._fetch_public_data", return_value=_MOCK_STATIONS):
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
    """07:30 UTC → ECMWF run 06 UTC (ultimo run ≤ 07:30 tra [0, 6, 12, 18])."""
    now = datetime(2026, 5, 15, 7, 30, tzinfo=UTC)
    ts_run = _infer_ts_run("ecmwf_ifs", now)
    assert ts_run == datetime(2026, 5, 15, 6, 0, tzinfo=UTC)


def test_infer_ts_run_ecmwf_after_noon() -> None:
    """14:00 UTC → ECMWF run 12 UTC."""
    now = datetime(2026, 5, 15, 14, 0, tzinfo=UTC)
    ts_run = _infer_ts_run("ecmwf_ifs", now)
    assert ts_run == datetime(2026, 5, 15, 12, 0, tzinfo=UTC)


def test_infer_ts_run_icon_eu_mid() -> None:
    """07:30 UTC → ICON-EU run 06 UTC (ogni 3h: 0,3,6,9,...)."""
    now = datetime(2026, 5, 15, 7, 30, tzinfo=UTC)
    ts_run = _infer_ts_run("icon_eu", now)
    assert ts_run == datetime(2026, 5, 15, 6, 0, tzinfo=UTC)


def test_infer_ts_run_midnight_edge() -> None:
    """00:30 UTC → ECMWF run 00 UTC."""
    now = datetime(2026, 5, 15, 0, 30, tzinfo=UTC)
    ts_run = _infer_ts_run("ecmwf_ifs", now)
    assert ts_run == datetime(2026, 5, 15, 0, 0, tzinfo=UTC)


def test_infer_ts_run_before_first_run() -> None:
    """00:00 UTC esatto → ECMWF run 00 UTC."""
    now = datetime(2026, 5, 15, 0, 0, tzinfo=UTC)
    ts_run = _infer_ts_run("ecmwf_ifs", now)
    assert ts_run == datetime(2026, 5, 15, 0, 0, tzinfo=UTC)


def test_parse_om_response_record_count() -> None:
    """_parse_om_response: 2 ore → 2 record."""
    records = _parse_om_response(
        _OM_MOCK_RESPONSE, "ecmwf_ifs", "lavoro_cosimo", _OM_TS_RUN_ECMWF
    )
    assert len(records) == 2


def test_parse_om_response_fields() -> None:
    """Verifica mapping variabili Open-Meteo → colonne wide."""
    records = _parse_om_response(
        _OM_MOCK_RESPONSE, "ecmwf_ifs", "lavoro_cosimo", _OM_TS_RUN_ECMWF
    )
    r = records[0]
    assert r["source"] == "open_meteo_ecmwf_ifs"
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
        _OM_MOCK_RESPONSE, "ecmwf_ifs", "lavoro_cosimo", _OM_TS_RUN_ECMWF
    )
    assert records[1]["lead_time_h"] == records[0]["lead_time_h"] + 1


def test_parse_om_response_empty_data() -> None:
    """Risposta senza hourly → lista vuota."""
    records = _parse_om_response({}, "ecmwf_ifs", "lavoro_cosimo", _OM_TS_RUN_ECMWF)
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
    records = _parse_om_response(data, "ecmwf_ifs", "lavoro_cosimo", _OM_TS_RUN_ECMWF)
    assert len(records) == 1
    assert records[0]["temp_c"] is None
    assert records[0]["precip_mm"] is None


_OM_MULTILEAD_RESPONSE: dict[str, Any] = {
    "hourly": {
        "time": ["2026-05-15T00:00", "2026-05-15T12:00"],
        "temperature_2m_previous_day1": [8.0, 20.0],
        "precipitation_previous_day1": [0.0, 1.5],
        "relative_humidity_2m_previous_day1": [80.0, 55.0],
        "wind_speed_10m_previous_day1": [1.0, 3.0],
        "temperature_2m_previous_day2": [7.5, 21.0],
        "precipitation_previous_day2": [0.0, 0.0],
        "relative_humidity_2m_previous_day2": [82.0, 50.0],
        "wind_speed_10m_previous_day2": [1.2, 3.5],
    }
}


def test_multilead_hourly_params_per_model() -> None:
    """Numero variabili = 4 × orizzonte del modello; gfs025 (orizzonte 0) → vuoto."""
    assert len(_multilead_hourly_params("ecmwf_ifs")) == 4 * 7
    assert len(_multilead_hourly_params("italia_meteo_arpae_icon_2i")) == 4 * 2
    assert _multilead_hourly_params("gfs025") == []


def test_parse_om_multilead_lead_and_ts_run() -> None:
    """Ogni previous_dayN → record a lead 24N con ts_run = mezzanotte(T − N giorni)."""
    # icon_2i ha orizzonte 2 → due lead (24h, 48h) per ogni ora valida.
    records = _parse_om_multilead(
        _OM_MULTILEAD_RESPONSE, "italia_meteo_arpae_icon_2i", "casa_campi"
    )
    assert len(records) == 4  # 2 ore × 2 lead
    by_lead = {(r["ts_valid"], r["lead_time_h"]): r for r in records}
    r1 = by_lead[(datetime(2026, 5, 15, 0, 0, tzinfo=UTC), 24)]
    assert r1["ts_run"] == datetime(2026, 5, 14, 0, 0, tzinfo=UTC)
    assert r1["source"] == "open_meteo_italia_meteo_arpae_icon_2i"
    assert r1["temp_c"] == pytest.approx(8.0)
    r2 = by_lead[(datetime(2026, 5, 15, 12, 0, tzinfo=UTC), 48)]
    assert r2["ts_run"] == datetime(2026, 5, 13, 0, 0, tzinfo=UTC)
    assert r2["temp_c"] == pytest.approx(21.0)


def test_parse_om_multilead_skips_all_null() -> None:
    """Ora con tutte le variabili null a un lead → record saltato."""
    data = {
        "hourly": {
            "time": ["2026-05-15T00:00"],
            "temperature_2m_previous_day1": [None],
            "precipitation_previous_day1": [None],
            "relative_humidity_2m_previous_day1": [None],
            "wind_speed_10m_previous_day1": [None],
        }
    }
    assert _parse_om_multilead(data, "arome_france", "casa_campi") == []


def test_parse_om_response_weather_code_as_int() -> None:
    """weather_code deve essere int, non float."""
    data = {
        "hourly": {
            "time": ["2026-05-15T08:00", "2026-05-15T09:00"],
            "temperature_2m": [18.5, 19.2],
            "relative_humidity_2m": [72.0, 68.0],
            "precipitation": [0.0, 0.2],
            "wind_speed_10m": [2.1, 3.4],
            "wind_direction_10m": [180.0, 190.0],
            "wind_gusts_10m": [5.0, 6.5],
            "surface_pressure": [1013.2, 1012.8],
            "weather_code": [3, 61],
        }
    }
    records = _parse_om_response(data, "ecmwf_ifs", "lavoro_cosimo", _OM_TS_RUN_ECMWF)
    assert len(records) == 2
    assert records[0]["weather_code"] == 3
    assert isinstance(records[0]["weather_code"], int)
    assert records[1]["weather_code"] == 61


def test_parse_om_response_weather_code_none_when_absent() -> None:
    """Senza weather_code nell'array → None nel record (campo opzionale)."""
    data = {
        "hourly": {
            "time": ["2026-05-15T08:00"],
            "temperature_2m": [18.5],
            "relative_humidity_2m": [72.0],
            "precipitation": [0.0],
            "wind_speed_10m": [2.1],
            "wind_direction_10m": [180.0],
            "wind_gusts_10m": [5.0],
            "surface_pressure": [1013.2],
            # weather_code assente
        }
    }
    records = _parse_om_response(data, "ecmwf_ifs", "lavoro_cosimo", _OM_TS_RUN_ECMWF)
    assert len(records) == 1
    assert records[0]["weather_code"] is None


def test_parse_om_response_weather_code_null_value() -> None:
    """weather_code=None nell'array → None nel record."""
    data = {
        "hourly": {
            "time": ["2026-05-15T08:00"],
            "temperature_2m": [18.5],
            "relative_humidity_2m": [72.0],
            "precipitation": [0.0],
            "wind_speed_10m": [2.1],
            "wind_direction_10m": [180.0],
            "wind_gusts_10m": [5.0],
            "surface_pressure": [1013.2],
            "weather_code": [None],
        }
    }
    records = _parse_om_response(data, "ecmwf_ifs", "lavoro_cosimo", _OM_TS_RUN_ECMWF)
    assert records[0]["weather_code"] is None


@pytest.fixture
def seeded_db_forecasts(tmp_path: Path) -> Path:
    db_path = tmp_path / "test_forecasts.duckdb"
    with DuckDBClient(db_path=db_path) as db:
        db.init_schema()
    return db_path


def test_upsert_forecasts_inserts(seeded_db_forecasts: Path) -> None:
    """upsert_forecasts: inserisce i record nella tabella forecasts."""
    records = _parse_om_response(
        _OM_MOCK_RESPONSE, "ecmwf_ifs", "lavoro_cosimo", _OM_TS_RUN_ECMWF
    )
    with DuckDBClient(db_path=seeded_db_forecasts) as db:
        n = db.upsert_forecasts(records)
        count = db.execute("SELECT COUNT(*) FROM forecasts").fetchone()[0]
    assert n == 2
    assert count == 2


def test_upsert_forecasts_idempotent(seeded_db_forecasts: Path) -> None:
    """Stesso batch inserito due volte → stessa riga, no duplicati."""
    records = _parse_om_response(
        _OM_MOCK_RESPONSE, "ecmwf_ifs", "lavoro_cosimo", _OM_TS_RUN_ECMWF
    )
    with DuckDBClient(db_path=seeded_db_forecasts) as db:
        db.upsert_forecasts(records)
        db.upsert_forecasts(records)
        count = db.execute("SELECT COUNT(*) FROM forecasts").fetchone()[0]
    assert count == 2


def test_upsert_forecasts_update_on_conflict(seeded_db_forecasts: Path) -> None:
    """Stesso (source, location, ts_run, ts_valid) con temp diversa → vince il secondo."""
    rec_v1 = _parse_om_response(
        _OM_MOCK_RESPONSE, "ecmwf_ifs", "lavoro_cosimo", _OM_TS_RUN_ECMWF
    )
    # Modifica la temperatura nel secondo batch
    import copy
    data_v2 = copy.deepcopy(_OM_MOCK_RESPONSE)
    data_v2["hourly"]["temperature_2m"] = [99.9, 99.9]
    rec_v2 = _parse_om_response(data_v2, "ecmwf_ifs", "lavoro_cosimo", _OM_TS_RUN_ECMWF)

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
        # ECMWF run 06: ts_valid 06:00-11:00 → ts_run=06:00
        # ECMWF run 12: ts_valid 12:00-17:00 → ts_run=12:00
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
    """ts_run=None → inferita per riga: 11:00 UTC → ts_run=06:00, 12:00 → ts_run=12:00."""
    records = _parse_om_response(_OM_HISTORICAL_MOCK, "ecmwf_ifs", "lavoro_cosimo", ts_run=None)
    assert len(records) == 3

    # ts_valid 11:00 → ECMWF run 06 UTC → lead_time_h = 5
    r11 = records[0]
    assert r11["ts_run"] == datetime(2026, 5, 14, 6, 0, tzinfo=UTC)
    assert r11["lead_time_h"] == 5

    # ts_valid 12:00 → ECMWF run 12 UTC → lead_time_h = 0
    r12 = records[1]
    assert r12["ts_run"] == datetime(2026, 5, 14, 12, 0, tzinfo=UTC)
    assert r12["lead_time_h"] == 0


def test_parse_om_response_historical_values() -> None:
    """Valori correttamente estratti in modalità storica."""
    records = _parse_om_response(_OM_HISTORICAL_MOCK, "ecmwf_ifs", "lavoro_cosimo", ts_run=None)
    r = records[0]
    assert r["temp_c"] == pytest.approx(20.0)
    assert r["humidity_pct"] == pytest.approx(50.0)
    assert r["wind_speed_ms"] == pytest.approx(3.0)
    assert r["pressure_hpa"] == pytest.approx(1012.0)


def test_upsert_forecasts_batch_historical(seeded_db_forecasts: Path) -> None:
    """upsert_forecasts batch: 3 record storici con ts_run diverse → 3 righe."""
    records = _parse_om_response(_OM_HISTORICAL_MOCK, "ecmwf_ifs", "lavoro_cosimo", ts_run=None)
    with DuckDBClient(db_path=seeded_db_forecasts) as db:
        n = db.upsert_forecasts(records)
        count = db.execute("SELECT COUNT(*) FROM forecasts").fetchone()[0]
    assert n == 3
    assert count == 3
    # verifica lead_time_h corretto per la riga con ts_valid=12:00
    ts_12 = datetime(2026, 5, 14, 12, 0, 0)  # UTC naive: forecasts stores UTC without tz
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
    from guazza.fetch_sir import fetch_sir_realtime
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
    with patch("guazza.fetch_sir.httpx.Client", return_value=mock_client):
        rec = fetch_sir_realtime("TOS99999999")
    assert rec["precip_mm"] == pytest.approx(3.4)
    assert rec["precip_interval_h"] == 1
    assert rec["granularity"] == "realtime"


# ═════════════════════════════════════════════════════════════════════════════
# _parse_sir_realtime_ts — timestamp da campo date
# ═════════════════════════════════════════════════════════════════════════════

def test_parse_sir_realtime_ts_from_termo() -> None:
    """SIR pubblica CET (UTC+1 fisso): 10:30 CET → 09:30 UTC naive."""
    from guazza.fetch_sir import _parse_sir_realtime_ts
    data = {"termo": {"value": "18.0", "date": "15/05/2026 10:30"}}
    ts = _parse_sir_realtime_ts(data)
    assert ts == datetime(2026, 5, 15, 9, 30)
    assert ts.tzinfo is None


def test_parse_sir_realtime_ts_fallback_now() -> None:
    """Senza campo date deve tornare un ts UTC naive vicino a now UTC."""
    from datetime import UTC

    from guazza.fetch_sir import _parse_sir_realtime_ts
    before = datetime.now(UTC).replace(tzinfo=None)
    ts = _parse_sir_realtime_ts({})
    after = datetime.now(UTC).replace(tzinfo=None)
    assert ts.tzinfo is None
    assert before <= ts <= after


def test_parse_sir_realtime_ts_unparsable_fallback() -> None:
    """Se date non è parsabile deve tornare un ts UTC naive vicino a now UTC."""
    from datetime import UTC

    from guazza.fetch_sir import _parse_sir_realtime_ts
    data = {"termo": {"value": "18.0", "date": "invalid-date"}}
    before = datetime.now(UTC).replace(tzinfo=None)
    ts = _parse_sir_realtime_ts(data)
    after = datetime.now(UTC).replace(tzinfo=None)
    assert ts.tzinfo is None
    assert before <= ts <= after


# ═════════════════════════════════════════════════════════════════════════════
# SIR bulk — _parse_sir_bulk_meta_ts, _parse_bulk_float, fetch_sir_bulk_realtime
# ═════════════════════════════════════════════════════════════════════════════

def test_parse_sir_bulk_meta_ts_standard() -> None:
    """SIR bulk è CET (UTC+1 fisso): 16:15 CET → 15:15 UTC naive."""
    from guazza.fetch_sir import _parse_sir_bulk_meta_ts
    ts = _parse_sir_bulk_meta_ts(" del 18/05/2026 16.15 (ora solare)")
    assert ts == datetime(2026, 5, 18, 15, 15)
    assert ts is not None and ts.tzinfo is None


def test_parse_sir_bulk_meta_ts_invalid() -> None:
    """Stringa non parsabile deve restituire None."""
    from guazza.fetch_sir import _parse_sir_bulk_meta_ts
    assert _parse_sir_bulk_meta_ts("") is None
    assert _parse_sir_bulk_meta_ts("nessun dato") is None


def test_parse_bulk_float_valid() -> None:
    from guazza.fetch_sir import _parse_bulk_float
    assert _parse_bulk_float("18") == 18.0
    assert _parse_bulk_float("-5") == -5.0
    assert _parse_bulk_float("0") == 0.0
    assert _parse_bulk_float("64") == 64.0


def test_parse_bulk_float_invalid() -> None:
    from guazza.fetch_sir import _parse_bulk_float
    assert _parse_bulk_float(None) is None
    assert _parse_bulk_float("") is None
    assert _parse_bulk_float("&nbsp;+&nbsp;") is None
    assert _parse_bulk_float("&nbsp;-&nbsp;") is None
    assert _parse_bulk_float("+") is None
    assert _parse_bulk_float("-") is None


def test_fetch_sir_bulk_realtime_filters_stations(monkeypatch: Any) -> None:
    """Deve filtrare solo le stazioni richieste e restituire record wide corretti."""
    from guazza.fetch_sir import fetch_sir_bulk_realtime

    # 16:15 CET (UTC+1) → 15:15 UTC naive
    fake_ts = datetime(2026, 5, 18, 15, 15)
    # Ogni endpoint ha Valore diverso per verificare il merge corretto
    _action_values = {"TERMO24": "20", "IGRO24": "65", "ANEMO24": "3", "PLUVIO": "0"}

    def _fake_bulk(action: str) -> dict[str, Any]:
        val = _action_values.get(action, "0")
        entry_a = {"IDStazione": "TOS01000001", "Valore": val, "Direzione": "180"}
        entry_b = {"IDStazione": "TOS01000002", "Valore": val, "Direzione": "90"}
        entry_skip = {"IDStazione": "TOS99999999", "Valore": "10"}
        return {"meta": " del 18/05/2026 16.15 (ora solare)", "data": [entry_a, entry_b, entry_skip]}

    monkeypatch.setattr("guazza.fetch_sir._fetch_sir_bulk_json", _fake_bulk)

    results = fetch_sir_bulk_realtime({"TOS01000001", "TOS01000002"})

    assert "TOS01000001" in results
    assert "TOS01000002" in results
    assert "TOS99999999" not in results

    rec = results["TOS01000001"]
    assert rec["source"] == "sir_toscana"
    assert rec["granularity"] == "realtime"
    assert rec["ts"] == fake_ts
    assert rec["temp_c"] == 20.0
    assert rec["humidity_pct"] == 65.0
    assert rec["wind_speed_ms"] == 3.0
    assert rec["wind_dir_deg"] == 180.0
    assert rec["precip_mm"] == 0.0


def test_fetch_sir_bulk_realtime_handles_offline_station(monkeypatch: Any) -> None:
    """Stazione offline (&nbsp;+&nbsp;) deve dare temp_c=None, non errore."""
    from guazza.fetch_sir import fetch_sir_bulk_realtime

    def _fake_bulk(action: str) -> dict[str, Any]:
        entry = {"IDStazione": "TOS01000001", "Valore": "&nbsp;+&nbsp;", "Direzione": "0"}
        return {"meta": " del 18/05/2026 16.15 (ora solare)", "data": [entry]}

    monkeypatch.setattr("guazza.fetch_sir._fetch_sir_bulk_json", _fake_bulk)

    results = fetch_sir_bulk_realtime({"TOS01000001"})
    assert "TOS01000001" in results
    assert results["TOS01000001"]["temp_c"] is None


# ═════════════════════════════════════════════════════════════════════════════
# ARPAT OpenData NRT -- _parse_arpat_nrt + fetch_arpat_nrt_station + fetch_arpat_all_locations
# ═════════════════════════════════════════════════════════════════════════════

from guazza.fetch_arpat import (  # noqa: E402
    _parse_arpat_nrt,
    fetch_arpat_all_locations,
    fetch_arpat_nrt_station,
)

_LOC_ID = "casa_campi"


def _make_arpat_entry(
    ora: str,
    data: str = "22-MAY-26",
    **params: float | None,
) -> dict[str, Any]:
    """Record orario ARPAT NRT con i parametri specificati."""
    entry: dict[str, Any] = {
        "ORA": ora,
        "DATA_OSSERVAZIONE": data,
        "NOME_STAZIONE": "FI-FIGLINE",
        "PROVINCIA": "FIRENZE",
        "COMUNE": "FIGLINE E INCISA VALDARNO",
        "VALIDAZIONE": "OPERATORE_PRIMO_LIVELLO",
    }
    for k, v in params.items():
        entry[k.upper().replace("_", ".")] = v
    return entry


# -- _parse_arpat_nrt ----------------------------------------------------------

def test_parse_arpat_nrt_single_hour_no2() -> None:
    """ARPAT NRT è ora locale (CEST in estate, UTC+2): 14:00 CEST → 12:00 UTC naive."""
    payload = [_make_arpat_entry("14", NO2=25.3)]
    records = _parse_arpat_nrt(payload, "FI-FIGLINE", _LOC_ID, 0.8)

    assert len(records) == 1
    r = records[0]
    assert r["source"] == "arpat"
    assert r["station_id"] == "FI-FIGLINE"
    assert r["location_id"] == _LOC_ID
    assert r["granularity"] == "hourly"
    assert r["weight"] == 0.8
    assert r["ts"] == datetime(2026, 5, 22, 12, 0)  # 14:00 CEST (UTC+2) → 12:00 UTC
    assert r["no2_ugm3"] == pytest.approx(25.3)


def test_parse_arpat_nrt_multi_param() -> None:
    """Record con più parametri -> tutti mappati su colonne wide."""
    payload = [_make_arpat_entry("08", NO2=18.0, O3=40.0, PM10=30.0, BENZENE=1.2)]
    records = _parse_arpat_nrt(payload, "FI-FIGLINE", _LOC_ID, 1.0)

    assert len(records) == 1
    r = records[0]
    assert r["no2_ugm3"] == pytest.approx(18.0)
    assert r["o3_ugm3"] == pytest.approx(40.0)
    assert r["pm10_ugm3"] == pytest.approx(30.0)
    assert r["benzene_ugm3"] == pytest.approx(1.2)


def test_parse_arpat_nrt_null_values_skipped() -> None:
    """Parametri null -> non inclusi nel record, ma record emesso se almeno uno non null."""
    payload = [{"ORA": "10", "DATA_OSSERVAZIONE": "22-MAY-26", "NO2": 15.0, "PM10": None}]
    records = _parse_arpat_nrt(payload, "FI-FIGLINE", _LOC_ID, 1.0)

    assert len(records) == 1
    assert "pm10_ugm3" not in records[0] or records[0].get("pm10_ugm3") is None
    assert records[0]["no2_ugm3"] == pytest.approx(15.0)


def test_parse_arpat_nrt_all_null_no_record() -> None:
    """Tutti i parametri null -> nessun record emesso."""
    payload = [{"ORA": "03", "DATA_OSSERVAZIONE": "22-MAY-26", "NO2": None, "PM10": None}]
    records = _parse_arpat_nrt(payload, "FI-FIGLINE", _LOC_ID, 1.0)
    assert records == []


def test_parse_arpat_nrt_unknown_param_ignored() -> None:
    """Parametri non mappati (H2S, BC, BB) -> ignorati, record emesso con quelli noti."""
    payload = [{"ORA": "06", "DATA_OSSERVAZIONE": "22-MAY-26", "H2S": 5.0, "BB": 10.0, "NO2": 20.0}]
    records = _parse_arpat_nrt(payload, "FI-FIGLINE", _LOC_ID, 1.0)

    assert len(records) == 1
    assert "h2s" not in records[0]
    assert records[0]["no2_ugm3"] == pytest.approx(20.0)


def test_parse_arpat_nrt_invalid_timestamp_skipped() -> None:
    """Timestamp malformato -> entry saltata."""
    payload = [
        {"ORA": "XX", "DATA_OSSERVAZIONE": "22-MAY-26", "NO2": 10.0},
        {"ORA": "12", "DATA_OSSERVAZIONE": "22-MAY-26", "NO2": 20.0},
    ]
    records = _parse_arpat_nrt(payload, "FI-FIGLINE", _LOC_ID, 1.0)
    assert len(records) == 1
    assert records[0]["ts"].hour == 10  # 12:00 CEST (UTC+2) → 10:00 UTC


def test_parse_arpat_nrt_empty_payload() -> None:
    """Payload lista vuota -> []."""
    assert _parse_arpat_nrt([], "FI-FIGLINE", _LOC_ID, 1.0) == []


def test_parse_arpat_nrt_non_list_payload() -> None:
    """Payload non-lista (dict, None) -> []."""
    assert _parse_arpat_nrt({}, "FI-FIGLINE", _LOC_ID, 1.0) == []
    assert _parse_arpat_nrt(None, "FI-FIGLINE", _LOC_ID, 1.0) == []


def test_parse_arpat_nrt_multi_hours() -> None:
    """Più ore -> un record per ora."""
    payload = [
        {"ORA": "10", "DATA_OSSERVAZIONE": "22-MAY-26", "NO2": 10.0},
        {"ORA": "11", "DATA_OSSERVAZIONE": "22-MAY-26", "NO2": 12.0},
        {"ORA": "12", "DATA_OSSERVAZIONE": "22-MAY-26", "NO2": 11.0},
    ]
    records = _parse_arpat_nrt(payload, "FI-FIGLINE", _LOC_ID, 1.0)
    assert len(records) == 3
    hours = sorted(r["ts"].hour for r in records)
    assert hours == [8, 9, 10]  # 10/11/12 CEST (UTC+2) → 8/9/10 UTC


def test_parse_arpat_nrt_co_factor_one() -> None:
    """CO ARPAT in mg/m³ (D.Lgs.155/2010) -> fattore 1.0, nessuna conversione."""
    payload = [{"ORA": "09", "DATA_OSSERVAZIONE": "22-MAY-26", "CO": 0.7}]
    records = _parse_arpat_nrt(payload, "FI-FIGLINE", _LOC_ID, 1.0)
    assert records[0]["co_mgm3"] == pytest.approx(0.7)


# -- fetch_arpat_nrt_station ---------------------------------------------------

def test_fetch_arpat_nrt_station_ok() -> None:
    """HTTP ok -> lista record; _log_scrape emesso con 'ok'."""
    payload = [{"ORA": "14", "DATA_OSSERVAZIONE": "22-MAY-26", "NO2": 30.0}]
    with patch("guazza.fetch_arpat._fetch_arpat_nrt_json", return_value=payload):
        records = fetch_arpat_nrt_station("FI-FIGLINE", _LOC_ID, 0.8)
    assert len(records) == 1
    assert records[0]["no2_ugm3"] == pytest.approx(30.0)


def test_fetch_arpat_nrt_station_http_error_returns_empty() -> None:
    """Errore HTTP -> lista vuota, nessuna eccezione propagata."""
    with patch("guazza.fetch_arpat._fetch_arpat_nrt_json", side_effect=Exception("404")):
        records = fetch_arpat_nrt_station("FI-FIGLINE", _LOC_ID, 0.8)
    assert records == []


# -- fetch_arpat_all_locations -------------------------------------------------

def test_fetch_arpat_all_locations_gate() -> None:
    """Location senza 'aria_qualita' in extras -> saltata."""
    locations = {
        "casa_campi": {"extras": ["aria_qualita"], "arpat_stations": [{"id": "FI-SIGNA", "weight": 1.0}]},
        "no_aria": {"extras": [], "arpat_stations": [{"id": "FI-FIGLINE", "weight": 1.0}]},
    }
    payload = [{"ORA": "10", "DATA_OSSERVAZIONE": "22-MAY-26", "NO2": 20.0}]
    with patch("guazza.fetch_arpat._fetch_arpat_nrt_json", return_value=payload):
        results = fetch_arpat_all_locations(locations)

    assert "casa_campi" in results
    assert "no_aria" not in results


def test_fetch_arpat_all_locations_dedup_station() -> None:
    """Stessa stazione in due location -> fetchata una sola volta (seen_stations)."""
    locations = {
        "loc_a": {"extras": ["aria_qualita"], "arpat_stations": [{"id": "FI-SIGNA", "weight": 1.0}]},
        "loc_b": {"extras": ["aria_qualita"], "arpat_stations": [{"id": "FI-SIGNA", "weight": 0.5}]},
    }
    payload = [{"ORA": "10", "DATA_OSSERVAZIONE": "22-MAY-26", "NO2": 20.0}]
    with patch("guazza.fetch_arpat._fetch_arpat_nrt_json", return_value=payload) as mock_fetch:
        fetch_arpat_all_locations(locations)
    assert mock_fetch.call_count == 1


# -- _parse_arpat_bollettino + fetch_arpat_bollettino_all_locations ────────────

from guazza.fetch_arpat import (  # noqa: E402
    _parse_arpat_bollettino,
    fetch_arpat_bollettino_all_locations,
)

_BOLL_MAP: dict[str, tuple[str, float]] = {
    "FI-SIGNA": ("casa_campi", 1.0),
    "FI-SCANDICCI": ("lavoro_cosimo", 0.8),
}

_BOLL_ENTRY = {"NOME_STAZIONE": "FI-SIGNA", "DATA_OSSERVAZIONE": "20-MAY-26", "PM10": 18, "PM2dot5": 9}


def test_parse_arpat_bollettino_happy_path() -> None:
    """Stazione configurata con PM10 e PM2.5 -> un record daily."""
    records = _parse_arpat_bollettino([_BOLL_ENTRY], _BOLL_MAP)
    assert len(records) == 1
    r = records[0]
    assert r["station_id"] == "FI-SIGNA"
    assert r["location_id"] == "casa_campi"
    assert r["granularity"] == "daily"
    assert r["pm10_ugm3"] == pytest.approx(18.0)
    assert r["pm25_ugm3"] == pytest.approx(9.0)
    assert r["ts"].hour == 0 and r["ts"].minute == 0


def test_parse_arpat_bollettino_station_not_in_map() -> None:
    """Stazione non configurata -> scartata."""
    entry = {"NOME_STAZIONE": "LU-CAPANNORI", "DATA_OSSERVAZIONE": "20-MAY-26", "PM10": 20, "PM2dot5": 10}
    records = _parse_arpat_bollettino([entry], _BOLL_MAP)
    assert records == []


def test_parse_arpat_bollettino_dash_values() -> None:
    """'-' e 'n.d.' -> None; se entrambi None record scartato."""
    entry = {"NOME_STAZIONE": "FI-SIGNA", "DATA_OSSERVAZIONE": "20-MAY-26", "PM10": "-", "PM2dot5": "n.d."}
    records = _parse_arpat_bollettino([entry], _BOLL_MAP)
    assert records == []


def test_parse_arpat_bollettino_partial_values() -> None:
    """PM10 presente, PM2.5 assente -> record con solo PM10."""
    entry = {"NOME_STAZIONE": "FI-SIGNA", "DATA_OSSERVAZIONE": "20-MAY-26", "PM10": 22}
    records = _parse_arpat_bollettino([entry], _BOLL_MAP)
    assert len(records) == 1
    assert records[0]["pm10_ugm3"] == pytest.approx(22.0)
    assert records[0]["pm25_ugm3"] is None


def test_parse_arpat_bollettino_invalid_date() -> None:
    """DATA_OSSERVAZIONE non parsificabile -> record scartato."""
    entry = {"NOME_STAZIONE": "FI-SIGNA", "DATA_OSSERVAZIONE": "INVALID", "PM10": 20}
    records = _parse_arpat_bollettino([entry], _BOLL_MAP)
    assert records == []


def test_parse_arpat_bollettino_non_list_payload() -> None:
    """Payload non-lista -> []."""
    assert _parse_arpat_bollettino({}, _BOLL_MAP) == []
    assert _parse_arpat_bollettino(None, _BOLL_MAP) == []


def test_parse_arpat_bollettino_multi_stations() -> None:
    """Più stazioni configurate -> un record per stazione presente."""
    payload = [
        {"NOME_STAZIONE": "FI-SIGNA",     "DATA_OSSERVAZIONE": "20-MAY-26", "PM10": 18, "PM2dot5": 9},
        {"NOME_STAZIONE": "FI-SCANDICCI", "DATA_OSSERVAZIONE": "20-MAY-26", "PM10": 25, "PM2dot5": 12},
        {"NOME_STAZIONE": "LU-CAPANNORI", "DATA_OSSERVAZIONE": "20-MAY-26", "PM10": 30},
    ]
    records = _parse_arpat_bollettino(payload, _BOLL_MAP)
    assert len(records) == 2
    ids = {r["station_id"] for r in records}
    assert ids == {"FI-SIGNA", "FI-SCANDICCI"}


def test_fetch_arpat_bollettino_all_locations_ok() -> None:
    """HTTP ok -> lista record dai record daily."""
    locations = {
        "casa_campi": {"extras": ["aria_qualita"], "arpat_stations": [{"id": "FI-SIGNA", "weight": 1.0}]},
    }
    payload = [{"NOME_STAZIONE": "FI-SIGNA", "DATA_OSSERVAZIONE": "20-MAY-26", "PM10": 18, "PM2dot5": 9}]
    with patch("guazza.fetch_arpat._fetch_arpat_bollettino_json", return_value=payload):
        records = fetch_arpat_bollettino_all_locations(locations)
    assert len(records) == 1
    assert records[0]["granularity"] == "daily"


def test_fetch_arpat_bollettino_all_locations_http_error() -> None:
    """Errore HTTP -> lista vuota, nessuna eccezione propagata."""
    locations = {
        "casa_campi": {"extras": ["aria_qualita"], "arpat_stations": [{"id": "FI-SIGNA", "weight": 1.0}]},
    }
    with patch("guazza.fetch_arpat._fetch_arpat_bollettino_json", side_effect=Exception("500")):
        records = fetch_arpat_bollettino_all_locations(locations)
    assert records == []


def test_fetch_arpat_bollettino_no_aria_qualita() -> None:
    """Location senza 'aria_qualita' in extras -> nessuna chiamata HTTP, lista vuota."""
    locations = {"no_aria": {"extras": [], "arpat_stations": [{"id": "FI-SIGNA", "weight": 1.0}]}}
    with patch("guazza.fetch_arpat._fetch_arpat_bollettino_json") as mock_fetch:
        records = fetch_arpat_bollettino_all_locations(locations)
    assert records == []
    mock_fetch.assert_not_called()
