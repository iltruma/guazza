"""Job CLI: costruzione tabella features_daily (Sprint 3).

Uso:
    uv run python -m guazza.jobs.features build
    uv run python -m guazza.jobs.features build --db /path/to/guazza.duckdb
    uv run python -m guazza.jobs.features info
"""

from __future__ import annotations

import os
from pathlib import Path

import typer
from loguru import logger

from guazza._logging import setup_logging
from guazza.features import build_features_daily
from guazza.storage import DuckDBClient

app = typer.Typer(help="Feature engineering — tabella features_daily.")


@app.callback()
def _callback() -> None:
    setup_logging()

_DEFAULT_DB = Path(os.environ.get("DB_PATH", "/var/lib/guazza/guazza.duckdb"))
_DB_OPT = typer.Option(_DEFAULT_DB, "--db", help="Path file DuckDB")


@app.command("build")
def cmd_build(db_path: Path = _DB_OPT) -> None:
    """Costruisce (o ricostruisce) features_daily da forecasts + observations."""
    with DuckDBClient(db_path=db_path) as db:
        n = build_features_daily(db)
    logger.info(f"features.build: {n} righe in features_daily")
    typer.echo(f"Righe scritte: {n}")


@app.command("info")
def cmd_info(db_path: Path = _DB_OPT) -> None:
    """Mostra statistiche su features_daily."""
    with DuckDBClient(db_path=db_path, read_only=True) as db:
        try:
            rows = db.execute("""
                SELECT
                    location_id,
                    COUNT(*) as n_rows,
                    MIN(target_date) as first_date,
                    MAX(target_date) as last_date,
                    MIN(lead_time_h) as min_lead_h,
                    MAX(lead_time_h) as max_lead_h,
                    ROUND(100.0 * COUNT(target_tmin_c) / COUNT(*), 1) as pct_target_tmin,
                    ROUND(100.0 * COUNT(target_precip_mm) / COUNT(*), 1) as pct_target_precip
                FROM features_daily
                GROUP BY location_id
                ORDER BY location_id
            """).fetchdf()
        except Exception:  # noqa: BLE001
            typer.echo("features_daily non esiste. Esegui prima: features build")
            raise typer.Exit(1) from None
    typer.echo(rows.to_string(index=False))


if __name__ == "__main__":
    app()
