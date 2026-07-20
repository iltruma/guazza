"""Job CLI: costruzione tabella features_daily (Sprint 3).

Uso:
    uv run python -m guazza.jobs.features build
    uv run python -m guazza.jobs.features build --db /path/to/guazza.duckdb
    uv run python -m guazza.jobs.features info
"""

from __future__ import annotations

from pathlib import Path

import typer

from guazza._logging import setup_logging
from guazza.features import build_features_daily
from guazza.jobs._common import DB_OPTION, job_run
from guazza.storage import DuckDBClient

app = typer.Typer(help="Feature engineering — tabella features_daily.")


@app.callback()
def _callback() -> None:
    setup_logging()


@app.command("build")
def cmd_build(
    db_path: Path = DB_OPTION,
    dry_run: bool = typer.Option(False, "--dry-run", help="Conta le righe sorgente senza riscrivere features_daily"),
) -> None:
    """Costruisce (o ricostruisce) features_daily da forecasts + observations."""
    if dry_run:
        with DuckDBClient(db_path=db_path, read_only=True) as db:
            n_forecasts = db.execute("SELECT COUNT(*) FROM forecasts").fetchone()[0]
            n_obs = db.execute("SELECT COUNT(*) FROM observations WHERE granularity = 'daily'").fetchone()[0]
        typer.echo(f"[dry-run] forecasts={n_forecasts} obs_daily={n_obs} — nessuna scrittura.")
        return

    with job_run("job_features_build") as stats:
        with DuckDBClient(db_path=db_path) as db:
            n = build_features_daily(db)
        stats.rows = n
        stats.summary = f"{n} righe in features_daily"


@app.command("info")
def cmd_info(db_path: Path = DB_OPTION) -> None:
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
