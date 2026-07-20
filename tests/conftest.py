"""Shared pytest fixtures per l'intera suite di test Guazza."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest

from guazza.storage import DuckDBClient


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    """Path a un file DuckDB temporaneo (non inizializzato)."""
    return tmp_path / "test.duckdb"


@pytest.fixture
def db(tmp_path: Path) -> Generator[DuckDBClient]:
    """DuckDBClient aperto con schema inizializzato. Si chiude dopo il test."""
    with DuckDBClient(db_path=tmp_path / "test.duckdb") as client:
        client.init_schema()
        yield client


@pytest.fixture
def seeded_db(tmp_db: Path) -> Path:
    """Path a un DB con schema inizializzato e connessione già chiusa.

    Per test che aprono la propria connessione via DuckDBClient(db_path=...).
    """
    with DuckDBClient(db_path=tmp_db) as client:
        client.init_schema()
    return tmp_db
