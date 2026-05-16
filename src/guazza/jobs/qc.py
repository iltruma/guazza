"""Job CLI: calcolo flag qualità osservazioni SIR.

Uso:
    uv run python -m guazza.jobs.qc run
    uv run python -m guazza.jobs.qc run --db /path/to/guazza.duckdb
    uv run python -m guazza.jobs.qc report
"""

from __future__ import annotations

import os
from pathlib import Path

import typer
from loguru import logger

from guazza.qc import compute_quality_flags
from guazza.storage import DuckDBClient

app = typer.Typer(help="Quality control osservazioni SIR.")

_DEFAULT_DB = Path(os.environ.get("DB_PATH", "/var/lib/guazza/guazza.duckdb"))
_DB_OPT = typer.Option(_DEFAULT_DB, "--db", help="Path file DuckDB")


@app.command("run")
def cmd_run(db_path: Path = _DB_OPT) -> None:
    """Ricalcola tutti i flag di qualità e li scrive in quality_flags."""
    with DuckDBClient(db_path=db_path) as db:
        n = compute_quality_flags(db)
    logger.info(f"qc.run: {n} flag totali in quality_flags")
    typer.echo(f"Flag inseriti: {n}")


@app.command("report")
def cmd_report(db_path: Path = _DB_OPT) -> None:
    """Stampa riepilogo flag per tipo e stazione."""
    with DuckDBClient(db_path=db_path, read_only=True) as db:
        rows = db.execute("""
            SELECT flag_type, station_id, COUNT(*) as n
            FROM quality_flags
            GROUP BY flag_type, station_id
            ORDER BY flag_type, n DESC
        """).fetchdf()
    if rows.empty:
        typer.echo("Nessun flag — esegui prima 'qc run'.")
        return
    typer.echo(rows.to_string(index=False))


if __name__ == "__main__":
    app()
