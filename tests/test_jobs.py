"""Test unitari per jobs/ingest.py — tutti i comandi mockati (no HTTP, no DB reale)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from guazza.jobs.ingest import (
    _all_sir_station_ids,
    _idst_for_station,
    _location_id_for_station,
    app,
)

runner = CliRunner()

# ── Fixtures config minimali ──────────────────────────────────────────────────

_LOCATIONS: dict = {
    "casa_campi": {
        "lat": 43.82, "lon": 11.13, "elevation_m": 42,
        "sir_station_id": "TOS01001225",
        "sir_stations": {
            "termo":  ["TOS01001225"],
            "pluvio": ["TOS01001225"],
        },
        "sir_idro_id": "TOS01004791",
    },
    "lavoro_cosimo": {
        "lat": 43.75, "lon": 11.17, "elevation_m": 42,
        "sir_station_id": "TOS01001215",
        "sir_stations": {
            "termo":  ["TOS01001215"],
            "pluvio": ["TOS01001215"],
        },
        "sir_idro_id": "TOS01004731",
    },
}

_STATIONS: dict = {
    "sir_idst_map": {
        "termometro": "termo_csv",
        "pluviometro": "pluvio0_24",
        "igrometro": "igro0_24",
        "anemometro": "anemo0_24",
        "idrometro": "idro_l",
    },
    "sir_stations": {
        "TOS01001225": {
            "nome": "Case Passerini",
            "lat": 43.81975, "lon": 11.16823, "quota_m": 33,
            "hysto": True,
            "sensors": ["termometro", "pluviometro", "igrometro", "anemometro"],
        },
        "TOS01001215": {
            "nome": "S. Giusto",
            "lat": 43.75995, "lon": 11.19376, "quota_m": 42,
            "hysto": True,
            "sensors": ["termometro", "pluviometro"],
        },
        "TOS01004791": {
            "nome": "S. Piero a Ponti",
            "lat": 43.82, "lon": 11.15, "quota_m": 40,
            "hysto": True,
            "sensors": ["idrometro"],
        },
        "TOS01004731": {
            "nome": "Ponte di Scandicci",
            "lat": 43.75, "lon": 11.19, "quota_m": None,
            "hysto": True,
            "sensors": ["idrometro"],
        },
    },
}


@pytest.fixture
def cfg_dir(tmp_path: Path) -> Path:
    """Scrive locations.yaml e stations.yaml minimi in una dir temporanea."""
    import yaml
    (tmp_path / "locations.yaml").write_text(
        yaml.dump({"locations": _LOCATIONS})
    )
    (tmp_path / "stations.yaml").write_text(yaml.dump(_STATIONS))
    return tmp_path


# ── Helper: _all_sir_station_ids ──────────────────────────────────────────────

def test_all_sir_station_ids() -> None:
    ids = _all_sir_station_ids(_LOCATIONS)
    assert "TOS01001225" in ids
    assert "TOS01001215" in ids
    assert "TOS01004791" in ids   # idro casa_campi
    assert "TOS01004731" in ids   # idro lavoro_cosimo


def test_idst_for_station_meteo() -> None:
    idst = _idst_for_station("TOS01001225", _STATIONS)
    assert "termo_csv" in idst
    assert "pluvio0_24" in idst
    assert "igro0_24" in idst
    assert "anemo0_24" in idst


def test_idst_for_station_idro() -> None:
    idst = _idst_for_station("TOS01004791", _STATIONS)
    assert idst == ["idro_l"]


def test_idst_for_station_unknown() -> None:
    idst = _idst_for_station("TOS99999999", _STATIONS)
    assert idst == []


def test_location_id_for_station_found() -> None:
    loc = _location_id_for_station("TOS01001225", _LOCATIONS)
    assert loc == "casa_campi"


def test_location_id_for_station_idro() -> None:
    loc = _location_id_for_station("TOS01004791", _LOCATIONS)
    assert loc == "casa_campi"


def test_location_id_for_station_not_found() -> None:
    loc = _location_id_for_station("TOS99999999", _LOCATIONS)
    assert loc == ""


# ── dry-run: nessuna scrittura ────────────────────────────────────────────────

def test_historical_dry_run(cfg_dir: Path, tmp_path: Path) -> None:
    db = tmp_path / "test.duckdb"
    result = runner.invoke(app, [
        "historical", "--dry-run",
        "--db", str(db),
        "--config-dir", str(cfg_dir),
    ])
    assert result.exit_code == 0
    assert "dry-run" in result.output
    assert not db.exists()


def test_historical_dry_run_with_location_filter(cfg_dir: Path, tmp_path: Path) -> None:
    db = tmp_path / "test.duckdb"
    result = runner.invoke(app, [
        "historical", "--dry-run",
        "--location", "casa_campi",
        "--db", str(db),
        "--config-dir", str(cfg_dir),
    ])
    assert result.exit_code == 0
    assert "casa_campi" in result.output
    assert "lavoro_cosimo" not in result.output


def test_historical_dry_run_unknown_location(cfg_dir: Path, tmp_path: Path) -> None:
    db = tmp_path / "test.duckdb"
    result = runner.invoke(app, [
        "historical", "--dry-run",
        "--location", "location_inesistente",
        "--db", str(db),
        "--config-dir", str(cfg_dir),
    ])
    assert result.exit_code == 1
    assert "sconosciute" in result.output


def test_historical_only_sir_and_only_openmeteo_exclusive(cfg_dir: Path, tmp_path: Path) -> None:
    db = tmp_path / "test.duckdb"
    result = runner.invoke(app, [
        "historical",
        "--only-sir", "--only-openmeteo",
        "--db", str(db),
        "--config-dir", str(cfg_dir),
    ])
    assert result.exit_code == 1
    assert "mutualmente esclusivi" in result.output


def test_realtime_dry_run(cfg_dir: Path, tmp_path: Path) -> None:
    db = tmp_path / "test.duckdb"
    result = runner.invoke(app, [
        "realtime", "--dry-run",
        "--db", str(db),
        "--config-dir", str(cfg_dir),
    ])
    assert result.exit_code == 0
    assert "dry-run" in result.output
    assert not db.exists()


# ── happy path mockato ────────────────────────────────────────────────────────

def test_realtime_happy_path(cfg_dir: Path, tmp_path: Path) -> None:
    """realtime scrive osservazioni SIR e Netatmo."""
    db_path = tmp_path / "test.duckdb"

    sir_realtime = {
        "TOS01001225": {
            "source": "sir_toscana", "station_id": "TOS01001225",
            "ts": datetime(2026, 5, 15, 10, 30, tzinfo=UTC),
            "granularity": "realtime",
            "temp_c": 18.0,
            "level_m": 3.2,  # idro: richiesto dal merge per-stazione in cmd_realtime
        },
    }

    with (
        patch("guazza.jobs.ingest.fetch_sir_bulk_realtime", return_value={}),
        patch("guazza.jobs.ingest.fetch_sir_stations_realtime", return_value=sir_realtime),
        patch("guazza.jobs.ingest.fetch_netatmo_all_locations", return_value={}),
    ):
        result = runner.invoke(app, [
            "realtime",
            "--db", str(db_path),
            "--config-dir", str(cfg_dir),
            "--output-dir", str(tmp_path / "output"),
        ])

    assert result.exit_code == 0, result.output
    assert "completato" in result.output

    from guazza.storage import DuckDBClient
    with DuckDBClient(db_path=db_path, read_only=True) as db:
        count = db.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    assert count >= 1


def test_realtime_failure_exits_1(cfg_dir: Path, tmp_path: Path) -> None:
    """Se SIR realtime esplode il job esce con codice 1."""
    db_path = tmp_path / "test.duckdb"

    with patch(
        "guazza.jobs.ingest.fetch_sir_stations_realtime",
        side_effect=RuntimeError("connection refused"),
    ):
        result = runner.invoke(app, [
            "realtime",
            "--db", str(db_path),
            "--config-dir", str(cfg_dir),
        ])

    assert result.exit_code == 1
