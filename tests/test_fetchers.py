"""Test unitari per fetchers.py (SIR + Netatmo + Open-Meteo wide + ARPAT)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
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
    _discard_records,
    _fetch_one_model_historical,
    _fetch_one_model_multilead,
    _infer_ts_run,
    _multilead_hourly_params,
    _parse_om_multilead,
    _parse_om_response,
    fetch_openmeteo_historical_batch,
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


def _mock_httpx_client(resp: MagicMock) -> MagicMock:
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.get = MagicMock(return_value=resp)
    return client


def _patched_sir_fetch(station_id: str, sensor_type: str, csv_text: str, location_id: str = "") -> list[dict[str, Any]]:
    with patch("guazza.fetch_sir.httpx.Client", return_value=_mock_httpx_client(_mock_response(csv_text))):
        return cast(list[dict[str, Any]], fetch_sir_historical(station_id, sensor_type, location_id))


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

    with patch("guazza.fetch_sir.httpx.Client", return_value=_mock_httpx_client(mock_resp)):
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

    with patch("guazza.fetch_sir.httpx.Client", return_value=_mock_httpx_client(mock_resp)):
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
        count_obs = db.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
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
    """Numero variabili = 4 × orizzonte del modello."""
    assert len(_multilead_hourly_params("ecmwf_ifs")) == 4 * 7
    assert len(_multilead_hourly_params("italia_meteo_arpae_icon_2i")) == 4 * 2
    assert len(_multilead_hourly_params("arome_france")) == 4 * 1


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
    data_v2 = cast(dict[str, Any], copy.deepcopy(_OM_MOCK_RESPONSE))
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
    with patch("guazza.fetch_sir.httpx.Client", return_value=_mock_httpx_client(mock_resp)):
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


# ═══════════════════════════════════════════════════════════════════════════
# Open-Meteo — batch historical/multilead: callback e serializzazione
# ═══════════════════════════════════════════════════════════════════════════

# Risposta mock multilead per ecmwf_ifs (max_n=7): 1 ora valida, 2 lead
# (day1 e day2) per tenere il mock piccolo. I lead rimasti (3-7) non sono
# definiti → il parser li salta (all-None).
_OM_MULTILEAD_MOCK: dict[str, Any] = {
    "latitude": 43.76,
    "longitude": 11.19,
    "timezone": "UTC",
    "hourly": {
        "time": ["2026-05-20T12:00"],
        "temperature_2m_previous_day1": [22.0],
        "precipitation_previous_day1": [0.0],
        "relative_humidity_2m_previous_day1": [55.0],
        "wind_speed_10m_previous_day1": [2.5],
        "temperature_2m_previous_day2": [21.0],
        "precipitation_previous_day2": [0.0],
        "relative_humidity_2m_previous_day2": [58.0],
        "wind_speed_10m_previous_day2": [2.0],
        # day3-day7: assenti → tutti None → saltati dal parser
    },
}


def test_fetch_one_model_historical_calls_on_records_per_chunk() -> None:
    """on_records viene chiamato una volta per chunk (2 chunk → 2 chiamate)."""
    chunks = [("2026-05-01", "2026-05-03"), ("2026-05-04", "2026-05-06")]
    loc_ids = ["loc_a"]
    lats = [43.8]
    lons = [11.1]

    collected: list[list[dict[str, Any]]] = []

    with (
        patch("guazza.fetch_openmeteo._fetch_om_json_historical", return_value=_OM_HISTORICAL_MOCK),
        patch("time.sleep"),
    ):
        _fetch_one_model_historical(
            "ecmwf_ifs", chunks, loc_ids, lats, lons,
            on_records=lambda recs: collected.append(recs),
        )

    assert len(collected) == 2, f"attesi 2 call, ricevuti {len(collected)}"
    for call_recs in collected:
        assert len(call_recs) > 0, "ogni chunk deve produrre almeno un record"


def test_fetch_one_model_historical_on_records_none_does_not_raise() -> None:
    """_discard_records come on_records non solleva eccezioni."""
    chunks = [("2026-05-01", "2026-05-01")]
    loc_ids = ["loc_a"]
    lats = [43.8]
    lons = [11.1]

    with (
        patch("guazza.fetch_openmeteo._fetch_om_json_historical", return_value=_OM_HISTORICAL_MOCK),
        patch("time.sleep"),
    ):
        _fetch_one_model_historical(
            "ecmwf_ifs", chunks, loc_ids, lats, lons,
            on_records=_discard_records,
        )


def test_fetch_openmeteo_historical_batch_models_in_series() -> None:
    """2 modelli × 1 giorno (1 chunk) → 2 chiamate HTTP, on_records ≥ 2 volte."""
    locations: dict[str, Any] = {
        "loc_a": {"lat": 43.8, "lon": 11.1},
        "loc_b": {"lat": 43.9, "lon": 11.2},
    }
    models = ["ecmwf_ifs", "icon_eu"]
    # 2 location → risposta lista
    mock_response = [_OM_HISTORICAL_MOCK, _OM_HISTORICAL_MOCK]

    on_records_calls: list[list[dict[str, Any]]] = []

    with (
        patch("guazza.fetch_openmeteo._fetch_om_json_historical", return_value=mock_response) as mock_fetch,
        patch("time.sleep"),
    ):
        fetch_openmeteo_historical_batch(
            locations, "2026-05-01", "2026-05-01",
            models=models,
            on_records=lambda recs: on_records_calls.append(recs),
        )

    assert mock_fetch.call_count == 2, (
        f"attese 2 chiamate HTTP (1 per modello × 1 chunk), ricevute {mock_fetch.call_count}"
    )
    assert len(on_records_calls) >= 2, (
        f"on_records deve essere chiamato almeno 2 volte (una per modello), "
        f"ricevuto {len(on_records_calls)}"
    )


def test_fetch_openmeteo_historical_batch_no_on_records() -> None:
    """fetch_openmeteo_historical_batch senza on_records non solleva eccezioni."""
    locations: dict[str, Any] = {"loc_a": {"lat": 43.8, "lon": 11.1}}

    with (
        patch("guazza.fetch_openmeteo._fetch_om_json_historical", return_value=_OM_HISTORICAL_MOCK),
        patch("time.sleep"),
    ):
        fetch_openmeteo_historical_batch(
            locations, "2026-05-01", "2026-05-01",
            models=["ecmwf_ifs"],
        )


def test_fetch_one_model_multilead_calls_on_records() -> None:
    """_fetch_one_model_multilead con ecmwf_ifs chiama on_records e produce lead 24h/48h."""
    chunks = [("2026-05-20", "2026-05-20")]
    loc_ids = ["loc_a"]
    lats = [43.8]
    lons = [11.1]

    collected: list[list[dict[str, Any]]] = []

    with (
        patch("guazza.fetch_openmeteo._fetch_om_json_historical", return_value=_OM_MULTILEAD_MOCK),
        patch("time.sleep"),
    ):
        _fetch_one_model_multilead(
            "ecmwf_ifs", chunks, loc_ids, lats, lons,
            on_records=lambda recs: collected.append(recs),
        )

    assert len(collected) >= 1, "on_records deve essere chiamato almeno una volta"
    all_records = [r for batch in collected for r in batch]
    lead_values = {r["lead_time_h"] for r in all_records}
    # day1 → 24h, day2 → 48h (le uniche definite nel mock)
    assert 24 in lead_values, f"atteso lead 24h, trovati {lead_values}"
    assert 48 in lead_values, f"atteso lead 48h, trovati {lead_values}"

