"""Test unitari per sir_historical — endpoint CSV download.php.

Tutti i test sono offline: httpx.Client.get viene mockato.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from guazza.ingestion.sir_historical import fetch_station_csv

# ── Helper ────────────────────────────────────────────────────────────────────

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
    """Costruisce un CSV nel formato SIR con header block + dati."""
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


def _patched_fetch(station_id: str, sensor_type: str, csv_text: str, location_id: str = "") -> list:
    mock_resp = _mock_response(csv_text)
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.get = MagicMock(return_value=mock_resp)
    with patch("guazza.ingestion.sir_historical.httpx.Client", return_value=mock_client):
        return fetch_station_csv(station_id, sensor_type, location_id)


# ── Test termo_csv ────────────────────────────────────────────────────────────

def test_termo_csv_basic() -> None:
    """Parsing base termo_csv: tmax e tmin corretti."""
    csv_text = _make_csv(
        '"Max [°C]";"Min [°C]"',
        ["15/06/2024;28,5;14,2", "16/06/2024;31,0;16,8"],
    )
    records = _patched_fetch("TOS01001215", "termo_csv", csv_text, "lavoro_cosimo")

    assert len(records) == 4  # 2 giorni × 2 variabili
    tmax = [r for r in records if r["variable"] == "tmax_c"]
    tmin = [r for r in records if r["variable"] == "tmin_c"]

    assert tmax[0]["value"] == pytest.approx(28.5)
    assert tmax[1]["value"] == pytest.approx(31.0)
    assert tmin[0]["value"] == pytest.approx(14.2)
    assert tmin[1]["value"] == pytest.approx(16.8)


def test_termo_csv_empty_cell_is_missing() -> None:
    """Cella vuota in termo_csv → value=None, flag='missing'."""
    csv_text = _make_csv(
        '"Max [°C]";"Min [°C]"',
        ["01/01/1992;;"],
    )
    records = _patched_fetch("TOS01001215", "termo_csv", csv_text)

    assert len(records) == 2
    for r in records:
        assert r["value"] is None
        assert r["flag"] == "missing"


def test_termo_csv_metadata() -> None:
    """I record hanno source, station_id, location_id e ts corretti."""
    csv_text = _make_csv(
        '"Max [°C]";"Min [°C]"',
        ["20/03/2023;19,0;8,5"],
    )
    records = _patched_fetch("TOS01001215", "termo_csv", csv_text, "lavoro_cosimo")

    for r in records:
        assert r["source"] == "sir_toscana"
        assert r["station_id"] == "TOS01001215"
        assert r["location_id"] == "lavoro_cosimo"
        assert r["ts"] == datetime(2023, 3, 20)


# ── Test pluvio0_24 ───────────────────────────────────────────────────────────

def test_pluvio_flag_mapping() -> None:
    """Flag Tipo Dato mappati correttamente."""
    csv_text = _make_csv(
        '"Precipitazione [mm]";"Tipo Dato"',
        [
            "01/01/2024;5,2;V",
            "02/01/2024;0,0;N",
            "03/01/2024;1,0;P",
            "04/01/2024;3,0;R",
            "05/01/2024;0,5;I",
            "06/01/2024;@;@",
        ],
    )
    records = _patched_fetch("TOS01001215", "pluvio0_24", csv_text)

    flags = {r["ts"].day: r["flag"] for r in records}
    assert flags[1] == "ok"           # V
    assert flags[2] == "ok"           # N
    assert flags[3] == "ok"           # P
    assert flags[4] == "reconstructed"  # R
    assert flags[5] == "uncertain"    # I
    assert flags[6] == "missing"      # @


def test_pluvio_missing_flag_cell_empty() -> None:
    """Cella valore vuota → value=None, flag='missing' anche senza '@'."""
    csv_text = _make_csv(
        '"Precipitazione [mm]";"Tipo Dato"',
        ["10/05/2024;;"],
    )
    records = _patched_fetch("TOS01001215", "pluvio0_24", csv_text)
    assert records[0]["value"] is None
    assert records[0]["flag"] == "missing"


# ── Test igro0_24 ─────────────────────────────────────────────────────────────

def test_igro_three_variables() -> None:
    """igro0_24: tre variabili nell'ordine med, min, max."""
    csv_text = _make_csv(
        '"Med [%]";"Min [%]";"Max [%]"',
        ["12/04/2022;72,0;45,0;95,0"],
    )
    records = _patched_fetch("TOS01001215", "igro0_24", csv_text)
    assert len(records) == 3

    by_var = {r["variable"]: r["value"] for r in records}
    assert by_var["hum_med_pct"] == pytest.approx(72.0)
    assert by_var["hum_min_pct"] == pytest.approx(45.0)
    assert by_var["hum_max_pct"] == pytest.approx(95.0)


def test_igro_partial_missing() -> None:
    """igro0_24: solo Med disponibile, Min e Max vuoti → missing."""
    csv_text = _make_csv(
        '"Med [%]";"Min [%]";"Max [%]"',
        ["01/04/2000;97,0;;"],
    )
    records = _patched_fetch("TOS01001215", "igro0_24", csv_text)

    by_var = {r["variable"]: r for r in records}
    assert by_var["hum_med_pct"]["value"] == pytest.approx(97.0)
    assert by_var["hum_med_pct"]["flag"] == "ok"
    assert by_var["hum_min_pct"]["value"] is None
    assert by_var["hum_min_pct"]["flag"] == "missing"
    assert by_var["hum_max_pct"]["value"] is None
    assert by_var["hum_max_pct"]["flag"] == "missing"


# ── Test anemo0_24 ────────────────────────────────────────────────────────────

def test_anemo_column_order() -> None:
    """anemo0_24: ordine colonne è Vel Med, Dir Med, Vel Max."""
    csv_text = _make_csv(
        '"Vel Med [m/s]";"Dir Med";"Vel Max [m/s]"',
        ["01/01/2024;1,5;NE;6,2"],
    )
    records = _patched_fetch("TOS01001215", "anemo0_24", csv_text)
    by_var = {r["variable"]: r["value"] for r in records}

    assert by_var["wind_speed_ms"] == pytest.approx(1.5)
    assert by_var["wind_gust_ms"] == pytest.approx(6.2)
    assert by_var["wind_dir_deg"] == pytest.approx(45.0)  # NE


def test_anemo_all_8_directions() -> None:
    """Tutte le 8 direzioni principali sono parsate correttamente."""
    expected = {
        "N": 0.0, "NE": 45.0, "E": 90.0, "SE": 135.0,
        "S": 180.0, "SO": 225.0, "O": 270.0, "NO": 315.0,
    }
    for abbr, deg in expected.items():
        csv_text = _make_csv(
            '"Vel Med [m/s]";"Dir Med";"Vel Max [m/s]"',
            [f"01/06/2024;1,0;{abbr};3,0"],
        )
        records = _patched_fetch("TOS01001215", "anemo0_24", csv_text)
        by_var = {r["variable"]: r["value"] for r in records}
        assert by_var["wind_dir_deg"] == pytest.approx(deg), f"Direzione {abbr} sbagliata"


def test_anemo_unknown_direction_is_none() -> None:
    """Direzione sconosciuta → wind_dir_deg=None, non crash."""
    csv_text = _make_csv(
        '"Vel Med [m/s]";"Dir Med";"Vel Max [m/s]"',
        ["01/06/2024;1,0;XYZ;3,0"],
    )
    records = _patched_fetch("TOS01001215", "anemo0_24", csv_text)
    by_var = {r["variable"]: r for r in records}
    assert by_var["wind_dir_deg"]["value"] is None
    assert by_var["wind_dir_deg"]["flag"] == "missing"


# ── Test idro_l ───────────────────────────────────────────────────────────────

def test_idro_flag_col() -> None:
    """idro_l: flag colonna Tipo Dato parsata correttamente."""
    csv_text = _make_csv(
        '"Livello [m]";"Tipo Dato"',
        ["01/03/2023;1,23;V", "02/03/2023;1,45;R"],
    )
    records = _patched_fetch("TOS01004591", "idro_l", csv_text)
    assert records[0]["flag"] == "ok"
    assert records[1]["flag"] == "reconstructed"
    assert records[0]["value"] == pytest.approx(1.23)


# ── Test errori ───────────────────────────────────────────────────────────────

def test_unsupported_sensor_type() -> None:
    """sensor_type non supportato → ValueError immediato (no HTTP call)."""
    with pytest.raises(ValueError, match="non supportato"):
        fetch_station_csv("TOS01001215", "termo")  # vecchio IDST sbagliato


def test_empty_response_returns_empty_list() -> None:
    """CSV senza dati (solo header) → lista vuota, nessun crash."""
    csv_text = _make_csv('"Max [°C]";"Min [°C]"', [])
    records = _patched_fetch("TOS01001215", "termo_csv", csv_text)
    assert records == []


def test_invalid_date_row_skipped() -> None:
    """Riga con data non parsabile viene saltata senza crash."""
    csv_text = _make_csv(
        '"Max [°C]";"Min [°C]"',
        ["NOT_A_DATE;25,0;12,0", "15/06/2024;28,0;14,0"],
    )
    records = _patched_fetch("TOS01001215", "termo_csv", csv_text)
    # Solo la riga valida produce record
    assert len(records) == 2
    assert all(r["ts"] == datetime(2024, 6, 15) for r in records)
