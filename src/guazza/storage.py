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
        # Eseguiamo lo script intero: DuckDB accetta più statement separati da ";"
        # in una singola execute(). Split manuale su ";" è fragile con MACRO e commenti.
        if self._conn is None:
            raise RuntimeError("DuckDBClient non è nel context manager.")
        self._conn.execute(sql)
        logger.info("Schema applicato")

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

    def upsert_forecasts(self, records: list[dict]) -> int:
        """UPSERT batch wide per forecast Open-Meteo nella tabella forecasts.

        PK: (source, location_id, ts_run, ts_valid).
        DO UPDATE sovrascrive tutte le colonne meteo (l'ultimo run vince).
        Usa executemany per performance su backfill storico (140k+ righe).

        Returns:
            Numero di record processati.
        """
        if not records:
            return 0

        rows = [
            [
                rec["source"], rec["location_id"], rec["ts_run"], rec["ts_valid"],
                rec.get("lead_time_h"),
                rec.get("temp_c"), rec.get("humidity_pct"), rec.get("precip_mm"),
                rec.get("wind_speed_ms"), rec.get("wind_dir_deg"),
                rec.get("wind_gust_ms"), rec.get("pressure_hpa"),
            ]
            for rec in records
        ]
        self.executemany(
            """
            INSERT INTO forecasts
                (source, location_id, ts_run, ts_valid, lead_time_h,
                 temp_c, humidity_pct, precip_mm,
                 wind_speed_ms, wind_dir_deg, wind_gust_ms, pressure_hpa)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (source, location_id, ts_run, ts_valid) DO UPDATE SET
                lead_time_h   = excluded.lead_time_h,
                temp_c        = excluded.temp_c,
                humidity_pct  = excluded.humidity_pct,
                precip_mm     = excluded.precip_mm,
                wind_speed_ms = excluded.wind_speed_ms,
                wind_dir_deg  = excluded.wind_dir_deg,
                wind_gust_ms  = excluded.wind_gust_ms,
                pressure_hpa  = excluded.pressure_hpa
            """,
            rows,
        )
        logger.info(f"upsert_forecasts: {len(records)} record processati")
        return len(records)

    def upsert_sir_observations(self, records: list[dict]) -> int:
        """UPSERT wide per osservazioni SIR storiche.

        Ogni record è parziale (solo le colonne del sensore scaricato).
        Il DO UPDATE usa COALESCE per preservare valori già presenti:
        se la colonna è già non-NULL, non viene sovrascritta con NULL.

        Returns:
            Numero di record processati.
        """
        if not records:
            return 0

        # Solo le colonne che esistono nello schema (escluse PK e granularity)
        _obs_cols = [
            "tmax_c", "tmin_c", "temp_c",
            "humidity_pct",
            "precip_mm", "precip_interval_h",
            "wind_speed_ms", "wind_dir_deg", "wind_gust_ms",
            "pressure_hpa", "level_m",
            "pm10_ugm3", "pm25_ugm3", "no2_ugm3", "o3_ugm3",
            "weight", "qc_pass",
        ]

        coalesce_sets = ", ".join(
            f"{col} = COALESCE(excluded.{col}, observations.{col})"
            for col in _obs_cols
        )

        rows: list[list[Any]] = []
        for rec in records:
            # hum_med_pct → humidity_pct; hum_min/max non hanno colonna → ignorate.
            humidity = rec.get("hum_med_pct") if rec.get("hum_med_pct") is not None else rec.get("humidity_pct")
            rows.append([
                rec.get("source", "sir_toscana"),
                rec["station_id"],
                rec.get("location_id", ""),
                rec["ts"],
                rec["granularity"],  # obbligatorio — 'daily' | 'realtime' | 'hourly'
                rec.get("tmax_c"),
                rec.get("tmin_c"),
                rec.get("temp_c"),
                humidity,
                rec.get("precip_mm"),
                rec.get("precip_interval_h"),
                rec.get("wind_speed_ms"),
                rec.get("wind_dir_deg"),
                rec.get("wind_gust_ms"),
                rec.get("pressure_hpa"),
                rec.get("level_m"),
                rec.get("pm10_ugm3"),
                rec.get("pm25_ugm3"),
                rec.get("no2_ugm3"),
                rec.get("o3_ugm3"),
                rec.get("weight"),
                rec.get("qc_pass"),
            ])

        self.executemany(
            f"""
            INSERT INTO observations
                (source, station_id, location_id, ts, granularity,
                 tmax_c, tmin_c, temp_c,
                 humidity_pct, precip_mm, precip_interval_h,
                 wind_speed_ms, wind_dir_deg, wind_gust_ms,
                 pressure_hpa, level_m,
                 pm10_ugm3, pm25_ugm3, no2_ugm3, o3_ugm3,
                 weight, qc_pass)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (source, station_id, ts, granularity) DO UPDATE SET
                location_id   = COALESCE(excluded.location_id, observations.location_id),
                {coalesce_sets}
            """,
            rows,
        )
        logger.info(f"upsert_sir_observations: {len(records)} record processati")
        return len(records)


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
