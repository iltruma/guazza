"""Job CLI: aggregazione Netatmo realtime → daily (accumulo storico, Sprint 9+).

Uso:
    uv run python -m guazza.jobs.netatmo_daily              # ieri (Europe/Rome)
    uv run python -m guazza.jobs.netatmo_daily --day 2026-06-01
    uv run python -m guazza.jobs.netatmo_daily --all        # backfill accumulato
    uv run python -m guazza.jobs.netatmo_daily --dry-run

Schedulare a ~06:00 (dopo che il realtime del giorno precedente è completo).
Variabili d'ambiente: ``DB_PATH``, ``HEALTHCHECKS_URL`` (ping opzionale).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import typer

from guazza._logging import setup_logging
from guazza.fetch_common import ITALY_TZ as _LOCAL_TZ
from guazza.jobs._common import DB_OPTION, job_run
from guazza.netatmo_daily import aggregate_netatmo_daily
from guazza.storage import DuckDBClient

app = typer.Typer(help="Aggregazione Netatmo realtime → daily.")


@app.command("run")
def cmd_run(
    db_path: Path = DB_OPTION,
    day: str = typer.Option("", "--day", help="Giorno locale YYYY-MM-DD (default: ieri)"),
    all_days: bool = typer.Option(False, "--all", help="Backfill di tutti i giorni accumulati"),
    min_samples: int = typer.Option(6, "--min-samples", help="Minimo campioni temp_c per giorno/stazione"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Aggrega il realtime Netatmo in righe daily nella tabella observations."""
    setup_logging()

    if all_days and day:
        typer.echo("Errore: --all e --day sono mutualmente esclusivi.")
        raise typer.Exit(1)

    target_day: date | None
    if all_days:
        target_day = None
    elif day:
        target_day = date.fromisoformat(day)
    else:
        target_day = datetime.now(tz=_LOCAL_TZ).date() - timedelta(days=1)

    label = "tutti i giorni" if target_day is None else target_day.isoformat()
    typer.echo(f"Netatmo daily: {label}{' [dry-run]' if dry_run else ''}")

    with job_run("job_netatmo_daily") as stats:
        with DuckDBClient(db_path=db_path) as db:
            db.init_schema()
            summary = aggregate_netatmo_daily(
                db, target_day=target_day, min_samples=min_samples, dry_run=dry_run
            )
        stats.rows = summary["rows"]
        stats.summary = f"giorni:{summary['days']} righe:{summary['rows']}"


if __name__ == "__main__":
    app()
