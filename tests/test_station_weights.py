"""Test unitari per station_weights."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from guazza.storage.duckdb_client import DuckDBClient
from guazza.storage.station_weights import (
    _weight_from_precalc_dist,
    compute_station_weight,
    refresh_station_weights,
)


# ── Fixture ───────────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "test.duckdb"


@pytest.fixture
def seeded_db(tmp_db: Path) -> Path:
    """DB con schema e migrations applicate."""
    with DuckDBClient(db_path=tmp_db) as db:
        db.init_schema()
        db.run_migrations()
    return tmp_db


# Config minimale per i test: 1 location, 2 stazioni SIR, 1 Netatmo
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


# ── Test compute_station_weight ───────────────────────────────────────────────


def test_weight_same_position_sir() -> None:
    """Stazione coincidente con il target → weight = 1.0 per SIR."""
    w, dist, delta = compute_station_weight(43.82, 11.13, 42, 43.82, 11.13, 42, "sir")
    assert math.isclose(w, 1.0, rel_tol=1e-9)
    assert math.isclose(dist, 0.0, abs_tol=1e-9)
    assert math.isclose(delta, 0.0, abs_tol=1e-9)


def test_weight_same_position_netatmo() -> None:
    """Stazione coincidente con il target → weight = 0.4 per Netatmo (source penalty)."""
    w, _, _ = compute_station_weight(43.82, 11.13, 42, 43.82, 11.13, 42, "netatmo")
    assert math.isclose(w, 0.4, rel_tol=1e-9)


def test_weight_at_characteristic_distance() -> None:
    """A 3 km (scala distanza), il peso SIR deve essere exp(-1) ≈ 0.368."""
    # Sposta di ~3km in latitudine: 3km / 111km per grado ≈ 0.027 gradi
    w, dist, _ = compute_station_weight(43.82 + 0.027, 11.13, 42, 43.82, 11.13, 42, "sir")
    assert math.isclose(dist, 3.0, rel_tol=0.05)
    assert math.isclose(w, math.exp(-1), rel_tol=0.05)


def test_weight_at_characteristic_elevation() -> None:
    """A 100m di delta quota (scala quota), il peso SIR deve essere exp(-1) ≈ 0.368."""
    w, _, delta = compute_station_weight(43.82, 11.13, 142, 43.82, 11.13, 42, "sir")
    assert math.isclose(delta, 100.0, abs_tol=1e-9)
    assert math.isclose(w, math.exp(-1), rel_tol=1e-9)


def test_weight_decreases_with_distance() -> None:
    """Il peso deve diminuire al crescere della distanza."""
    w1, _, _ = compute_station_weight(43.82, 11.13, 42, 43.82, 11.13, 42, "sir")
    w2, _, _ = compute_station_weight(43.83, 11.13, 42, 43.82, 11.13, 42, "sir")
    w3, _, _ = compute_station_weight(43.90, 11.13, 42, 43.82, 11.13, 42, "sir")
    assert w1 > w2 > w3


def test_weight_sir_gt_netatmo_same_position() -> None:
    """A parità di posizione SIR deve pesare più di Netatmo."""
    w_sir, _, _ = compute_station_weight(43.82, 11.14, 42, 43.82, 11.13, 42, "sir")
    w_net, _, _ = compute_station_weight(43.82, 11.14, 42, 43.82, 11.13, 42, "netatmo")
    assert w_sir > w_net


def test_weight_from_precalc_dist_matches() -> None:
    """_weight_from_precalc_dist deve dare lo stesso risultato di compute_station_weight
    quando la distanza è pre-calcolata correttamente."""
    _, dist, _ = compute_station_weight(43.82, 11.14, 50, 43.82, 11.13, 42, "netatmo")
    w1, _, _ = compute_station_weight(43.82, 11.14, 50, 43.82, 11.13, 42, "netatmo")
    w2, _, _ = _weight_from_precalc_dist(dist, 50, 42, "netatmo")
    assert math.isclose(w1, w2, rel_tol=1e-6)


# ── Test refresh_station_weights ─────────────────────────────────────────────


def test_refresh_inserts_records(seeded_db: Path) -> None:
    """refresh_station_weights() inserisce i record attesi in DuckDB."""
    with DuckDBClient(db_path=seeded_db) as db:
        records = refresh_station_weights(db, _LOC, _STATIONS)
        count = db.execute("SELECT COUNT(*) FROM station_weights").fetchone()[0]

    assert len(records) == 2   # SIR_NEAR + SIR_FAR
    assert count == 2


def test_refresh_idempotent(seeded_db: Path) -> None:
    """Chiamare refresh due volte non duplica i record."""
    with DuckDBClient(db_path=seeded_db) as db:
        refresh_station_weights(db, _LOC, _STATIONS)
        refresh_station_weights(db, _LOC, _STATIONS)
        count = db.execute("SELECT COUNT(*) FROM station_weights").fetchone()[0]
    assert count == 2


def test_refresh_sources_correct(seeded_db: Path) -> None:
    """I record SIR hanno source='sir', quelli Netatmo 'netatmo'."""
    with DuckDBClient(db_path=seeded_db) as db:
        refresh_station_weights(db, _LOC, _STATIONS)
        sources = {
            row[0]
            for row in db.execute("SELECT DISTINCT source FROM station_weights").fetchall()
        }
    assert sources == {"sir"}


def test_refresh_near_heavier_than_far(seeded_db: Path) -> None:
    """La stazione vicina deve avere peso maggiore di quella lontana."""
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
    """Stazione SIR con quota_m null non deve causare eccezioni."""
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
    # Con quota null, delta_elev deve essere 0 (nessuna penalità)
    assert math.isclose(records[0]["delta_elev_m"], 0.0, abs_tol=1e-9)


def test_refresh_real_config(seeded_db: Path) -> None:
    """Smoke test con i config reali: deve produrre record per tutte e 4 le location."""
    from guazza.storage.station_weights import load_configs

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

    # Ogni location deve avere almeno una stazione SIR con peso > 0
    sir_by_loc = {r["location_id"]: r["weight"] for r in records if r["source"] == "sir"}
    for loc_id in loc_ids:
        assert sir_by_loc.get(loc_id, 0) > 0, f"Nessuna SIR con peso > 0 per {loc_id}"
