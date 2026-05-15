"""Test unitari per storage.py (DuckDB client wide)."""

from __future__ import annotations

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
    """Inserimento wide: una sola riga per (source, station_id, ts)."""
    with DuckDBClient(db_path=tmp_db) as db:
        db.init_schema()
        db.execute(
            """
            INSERT INTO observations
                (source, station_id, location_id, ts,
                 temp_c, humidity_pct, precip_mm)
            VALUES ('sir_toscana', 'TOS01001215', 'casa_campi',
                    '2024-06-15 00:00:00', 28.5, 65.0, 0.0)
            """,
        )
        row = db.execute(
            "SELECT temp_c, humidity_pct, precip_mm FROM observations"
        ).fetchone()
    assert row == (28.5, 65.0, 0.0)
