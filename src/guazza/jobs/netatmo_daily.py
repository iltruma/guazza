"""Job CLI: aggregazione Netatmo realtime → daily (accumulo storico, Sprint 9+).

Uso:
    uv run python -m guazza.jobs.netatmo_daily            # ieri (Europe/Rome)
    uv run python -m guazza.jobs.netatmo_daily --day 2026-06-01
    uv run python -m guazza.jobs.netatmo_daily --all      # backfill accumulato
    uv run python -m guazza.jobs.netatmo_daily --dry-run

Schedulare a ~06:00 (dopo che il realtime del giorno precedente è completo).
Variabili d'ambiente: ``DB_PATH``, ``HEALTHCHECKS_URL`` (ping opzionale).
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import typer
from loguru import logger

from guazza._logging import setup_logging
from guazza.netatmo_daily import aggregate_netatmo_daily
from guazza.storage import DuckDBClient

app = typer.Typer(help="Aggregazione Netatmo realtime → daily.")

_LOCAL_TZ = ZoneInfo("Europe/Rome")
_DEFAULT_DB = Path(os.environ.get("DB_PATH", "/var/lib/guazza/guazza.duckdb"))
_DB_OPT = typer.Option(_DEFAULT_DB, "--db", help="Path file DuckDB")


@app.callback()
def _callback() -> None:
    setup_logging()


def _ping_healthchecks(status: str = "") -> None:
    """Ping Healthchecks.io se HEALTHCHECKS_URL è configurato (altrimenti skip)."""
    base_url = os.environ.get("HEALTHCHECKS_URL", "").strip()
    if not base_url:
        return
    url = base_url + status
    try:
        httpx.get(url, timeout=5)
        logger.debug(f"Healthchecks ping: {url}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Healthchecks ping fallito: {e}")


@app.command("run")
def cmd_run(
    db_path: Path = _DB_OPT,
    day: str = typer.Option("", "--day", help="Giorno locale YYYY-MM-DD (default: ieri)"),
    all_days: bool = typer.Option(False, "--all", help="Backfill di tutti i giorni accumulati"),
    min_samples: int = typer.Option(6, "--min-samples", help="Minimo campioni temp_c per giorno/stazione"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Aggrega il realtime Netatmo in righe daily nella tabella observations."""
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

    _ping_healthchecks("/start")
    try:
        with DuckDBClient(db_path=db_path) as db:
            db.init_schema()
            summary = aggregate_netatmo_daily(
                db, target_day=target_day, min_samples=min_samples, dry_run=dry_run
            )
    except Exception as e:
        logger.error(f"netatmo_daily fallito: {e}")
        _ping_healthchecks("/fail")
        raise typer.Exit(1) from e

    _ping_healthchecks()
    typer.echo(
        f"completato — giorni:{summary['days']} righe:{summary['rows']}"
    )


if __name__ == "__main__":
    app()
