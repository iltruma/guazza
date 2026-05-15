"""DuckDB client con lock file per scritture serializzate.

Workaround KI001: DuckDB ammette un solo writer alla volta.
Usiamo fcntl.flock() su un lock file per serializzare le aperture in write mode.
In read-only mode il lock non è necessario.

Uso tipico:
    with DuckDBClient() as db:
        db.execute("INSERT INTO ...")
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import duckdb
import typer
from loguru import logger

_DEFAULT_DB_PATH = Path(os.environ.get("DB_PATH", "/var/lib/guazza/guazza.duckdb"))
_SCHEMA_SQL = Path(__file__).parent / "schema.sql"


class DuckDBClient:
    """Wrapper attorno a duckdb.connect con lock file per write serializzate."""

    def __init__(
        self,
        db_path: Path | str | None = None,
        read_only: bool = False,
    ) -> None:
        self.db_path = Path(db_path) if db_path else _DEFAULT_DB_PATH
        self.read_only = read_only
        self._conn: duckdb.DuckDBPyConnection | None = None
        self._lock_fd: int | None = None
        self._lock_path = self.db_path.with_suffix(".lock")

    def __enter__(self) -> DuckDBClient:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        if not self.read_only:
            self._lock_fd = os.open(str(self._lock_path), os.O_CREAT | os.O_WRONLY)
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX)
            logger.debug(f"Lock acquisito: {self._lock_path}")

        self._conn = duckdb.connect(str(self.db_path), read_only=self.read_only)
        logger.debug(f"DuckDB aperto: {self.db_path} (read_only={self.read_only})")
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

        if self._lock_fd is not None:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._lock_fd = None
            logger.debug(f"Lock rilasciato: {self._lock_path}")

    def execute(self, query: str, params: list[Any] | None = None) -> Any:
        """Esegui una query SQL. Richiede connessione aperta."""
        if self._conn is None:
            raise RuntimeError("DuckDBClient non è nel context manager.")
        if params:
            return self._conn.execute(query, params)
        return self._conn.execute(query)

    def executemany(self, query: str, params: list[list[Any]]) -> None:
        """Esegui la stessa query su una lista di parametri (batch insert)."""
        if self._conn is None:
            raise RuntimeError("DuckDBClient non è nel context manager.")
        self._conn.executemany(query, params)

    def init_schema(self) -> None:
        """Applica schema.sql al database (IF NOT EXISTS — idempotente)."""
        if not _SCHEMA_SQL.exists():
            raise FileNotFoundError(f"Schema SQL non trovato: {_SCHEMA_SQL}")
        sql = _SCHEMA_SQL.read_text()
        statements = [s.strip() for s in sql.split(";") if s.strip()]
        for stmt in statements:
            self.execute(stmt)
        logger.info(f"Schema applicato: {len(statements)} statement eseguiti")

    def verify_schema(self) -> bool:
        """Verifica che le tabelle attese esistano nel database."""
        expected_tables = {
            "locations",
            "observations",
            "forecasts",
            "predictions",
            "benchmark_forecasts",
            "alerts",
            "station_weights",
            "netatmo_fetch_log",
            "indicator_log",
        }
        result = self.execute("SHOW TABLES").fetchall()
        existing = {row[0] for row in result}
        missing = expected_tables - existing
        if missing:
            logger.error(f"Tabelle mancanti: {missing}")
            return False
        logger.info(f"Schema OK: {len(existing)} tabelle presenti")
        return True


@contextmanager
def open_db(
    db_path: Path | str | None = None,
    read_only: bool = False,
) -> Generator[DuckDBClient, None, None]:
    """Shortcut: `with open_db() as db:` invece di instanziare direttamente."""
    client = DuckDBClient(db_path=db_path, read_only=read_only)
    with client:
        yield client


# ── CLI entry point ───────────────────────────────────────────────────────────

app = typer.Typer(help="Utility DuckDB per Guazza.")

_DB_OPTION = typer.Option(_DEFAULT_DB_PATH, "--db", help="Path del file DuckDB")


@app.command("init-schema")
def cmd_init_schema(db_path: Path = _DB_OPTION) -> None:
    """Inizializza (o aggiorna) lo schema DuckDB."""
    with DuckDBClient(db_path=db_path) as db:
        db.init_schema()
    typer.echo("Schema inizializzato.")


@app.command("verify-schema")
def cmd_verify_schema(db_path: Path = _DB_OPTION) -> None:
    """Verifica che tutte le tabelle attese esistano."""
    with DuckDBClient(db_path=db_path, read_only=True) as db:
        ok = db.verify_schema()
    raise typer.Exit(0 if ok else 1)


if __name__ == "__main__":
    app()
