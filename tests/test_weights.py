"""Test unitari per weights.py."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from guazza.storage import DuckDBClient
from guazza.weights import (
    compute_station_weight,
    refresh_station_weights,
)


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "test.duckdb"


@pytest.fixture
def seeded_db(tmp_db: Path) -> Path:
    with DuckDBClient(db_path=tmp_db) as db:
        db.init_schema()
    return tmp_db


_LOC = {
    "test_loc": {
        "label": "Test Location",
        "lat": 43.82,
        "lon": 11.13,
        "elevation_m": 42,
        "sir_stations": {
            "termo": ["SIR_NEAR", "SIR_FAR"],
        },
    }
}

_STATIONS = {
    "sir_stations": {
        "SIR_NEAR": {"nome": "Stazione Vicina", "lat": 43.821, "lon": 11.131, "quota_m": 40},
        "SIR_FAR":  {"nome": "Stazione Lontana",  "lat": 43.90,  "lon": 11.25,  "quota_m": 200},
    },
}


def test_weight_same_position_sir() -> None:
    w, dist, delta = compute_station_weight(43.82, 11.13, 42, 43.82, 11.13, 42, "sir")
    assert math.isclose(w, 1.0, rel_tol=1e-9)
    assert math.isclose(dist, 0.0, abs_tol=1e-9)
    assert math.isclose(delta, 0.0, abs_tol=1e-9)


def test_weight_same_position_netatmo() -> None:
    w, _, _ = compute_station_weight(43.82, 11.13, 42, 43.82, 11.13, 42, "netatmo")
    assert math.isclose(w, 0.4, rel_tol=1e-9)


def test_weight_at_characteristic_distance() -> None:
    w, dist, _ = compute_station_weight(43.82 + 0.027, 11.13, 42, 43.82, 11.13, 42, "sir")
    assert math.isclose(dist, 3.0, rel_tol=0.05)
    assert math.isclose(w, math.exp(-1), rel_tol=0.05)


def test_weight_at_characteristic_elevation() -> None:
    w, _, delta = compute_station_weight(43.82, 11.13, 142, 43.82, 11.13, 42, "sir")
    assert math.isclose(delta, 100.0, abs_tol=1e-9)
    assert math.isclose(w, math.exp(-1), rel_tol=1e-9)


def test_weight_decreases_with_distance() -> None:
    w1, _, _ = compute_station_weight(43.82, 11.13, 42, 43.82, 11.13, 42, "sir")
    w2, _, _ = compute_station_weight(43.83, 11.13, 42, 43.82, 11.13, 42, "sir")
    w3, _, _ = compute_station_weight(43.90, 11.13, 42, 43.82, 11.13, 42, "sir")
    assert w1 > w2 > w3


def test_weight_sir_gt_netatmo_same_position() -> None:
    w_sir, _, _ = compute_station_weight(43.82, 11.14, 42, 43.82, 11.13, 42, "sir")
    w_net, _, _ = compute_station_weight(43.82, 11.14, 42, 43.82, 11.13, 42, "netatmo")
    assert w_sir > w_net


def test_refresh_inserts_records(seeded_db: Path) -> None:
    with DuckDBClient(db_path=seeded_db) as db:
        records = refresh_station_weights(db, _LOC, _STATIONS)
        count = db.execute("SELECT COUNT(*) FROM station_weights").fetchone()[0]
    assert len(records) == 2
    assert count == 2


def test_refresh_idempotent(seeded_db: Path) -> None:
    with DuckDBClient(db_path=seeded_db) as db:
        refresh_station_weights(db, _LOC, _STATIONS)
        refresh_station_weights(db, _LOC, _STATIONS)
        count = db.execute("SELECT COUNT(*) FROM station_weights").fetchone()[0]
    assert count == 2


def test_refresh_sources_correct(seeded_db: Path) -> None:
    with DuckDBClient(db_path=seeded_db) as db:
        refresh_station_weights(db, _LOC, _STATIONS)
        sources = {
            row[0]
            for row in db.execute("SELECT DISTINCT source FROM station_weights").fetchall()
        }
    assert sources == {"sir"}


def test_refresh_near_heavier_than_far(seeded_db: Path) -> None:
    with DuckDBClient(db_path=seeded_db) as db:
        refresh_station_weights(db, _LOC, _STATIONS)
        rows = {
            row[0]: row[1]
            for row in db.execute(
                "SELECT station_id, weight FROM station_weights WHERE source='sir'"
            ).fetchall()
        }
    assert rows["SIR_NEAR"] > rows["SIR_FAR"]


def test_refresh_null_quota_no_crash(seeded_db: Path) -> None:
    stations_with_null = {
        "sir_stations": {
            "SIR_NULL_QUOTA": {"nome": "Idro senza quota", "lat": 43.82, "lon": 11.15, "quota_m": None},
        },
        "netatmo_stations": {},
    }
    loc = {
        "loc_null": {
            "label": "Test null quota",
            "lat": 43.82, "lon": 11.13, "elevation_m": 42,
            "sir_stations": {"idro": ["SIR_NULL_QUOTA"]},
        }
    }
    with DuckDBClient(db_path=seeded_db) as db:
        records = refresh_station_weights(db, loc, stations_with_null)
    assert len(records) == 1
    assert math.isclose(records[0]["delta_elev_m"], 0.0, abs_tol=1e-9)


def test_refresh_real_config(seeded_db: Path) -> None:
    from guazza.weights import load_configs

    try:
        locations, stations = load_configs()
    except FileNotFoundError:
        pytest.skip("Config non disponibili in questo ambiente")

    with DuckDBClient(db_path=seeded_db) as db:
        records = refresh_station_weights(db, locations, stations)

    loc_ids = {r["location_id"] for r in records}
    assert "casa_campi" in loc_ids
    assert "lavoro_cosimo" in loc_ids
    assert "lavoro_madda" in loc_ids
    assert "casa_cesto" in loc_ids

    sir_by_loc = {r["location_id"]: r["weight"] for r in records if r["source"] == "sir"}
    for loc_id in loc_ids:
        assert sir_by_loc.get(loc_id, 0) > 0, f"Nessuna SIR con peso > 0 per {loc_id}"
