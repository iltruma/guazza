"""Test unitari per DuckDBClient."""

from __future__ import annotations

from pathlib import Path

import pytest

from guazza.storage.duckdb_client import DuckDBClient, open_db


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    """Restituisce un path temporaneo per il DB di test."""
    return tmp_path / "test_guazza.duckdb"


def test_init_schema(tmp_db: Path) -> None:
    """init_schema() crea tutte le tabelle attese."""
    with DuckDBClient(db_path=tmp_db) as db:
        db.init_schema()
        ok = db.verify_schema()
    assert ok


def test_init_schema_idempotent(tmp_db: Path) -> None:
    """init_schema() è idempotente (IF NOT EXISTS)."""
    with DuckDBClient(db_path=tmp_db) as db:
        db.init_schema()
        # Seconda chiamata non deve lanciare eccezioni
        db.init_schema()
        ok = db.verify_schema()
    assert ok


def test_verify_schema_empty_db(tmp_db: Path) -> None:
    """verify_schema() restituisce False su DB vuoto."""
    with DuckDBClient(db_path=tmp_db) as db:
        ok = db.verify_schema()
    assert not ok


def test_open_db_context_manager(tmp_db: Path) -> None:
    """open_db() funziona come context manager shortcut."""
    with open_db(db_path=tmp_db) as db:
        db.init_schema()
        result = db.execute("SELECT COUNT(*) FROM locations").fetchone()
    assert result is not None
    assert result[0] == 0


def test_execute_insert_select(tmp_db: Path) -> None:
    """Verifica insert + select su tabella locations."""
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
    """execute() fuori dal context manager lancia RuntimeError."""
    client = DuckDBClient(db_path=tmp_db)
    with pytest.raises(RuntimeError, match="context manager"):
        client.execute("SELECT 1")


def test_run_migrations_on_fresh_db(tmp_db: Path) -> None:
    """run_migrations() su DB con schema applicato esegue tutte le migrations pendenti."""
    with DuckDBClient(db_path=tmp_db) as db:
        db.init_schema()
        n = db.run_migrations()
    # v1 + v2 (drop cfr_station_id) + v3 (netatmo_fetch_log) + v4 (observations PK) = 4
    assert n == 4


def test_run_migrations_idempotent(tmp_db: Path) -> None:
    """run_migrations() è idempotente: seconda chiamata restituisce 0."""
    with DuckDBClient(db_path=tmp_db) as db:
        db.init_schema()
        db.run_migrations()
        n = db.run_migrations()
    assert n == 0


def test_migrations_add_columns(tmp_db: Path) -> None:
    """Migration v1 aggiunge weight/qc_pass a observations e alpha/cost_fn/cost_fp a indicator_log."""
    with DuckDBClient(db_path=tmp_db) as db:
        db.init_schema()
        db.run_migrations()

        obs_cols = {
            row[0]
            for row in db.execute("DESCRIBE observations").fetchall()
        }
        log_cols = {
            row[0]
            for row in db.execute("DESCRIBE indicator_log").fetchall()
        }

    assert "weight" in obs_cols
    assert "qc_pass" in obs_cols
    assert "alpha" in log_cols
    assert "cost_fn" in log_cols
    assert "cost_fp" in log_cols


def test_station_weights_table_exists(tmp_db: Path) -> None:
    """init_schema() crea la tabella station_weights."""
    with DuckDBClient(db_path=tmp_db) as db:
        db.init_schema()
        result = db.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'station_weights'"
        ).fetchone()
    assert result is not None
    assert result[0] == 1
