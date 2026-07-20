"""Job CLI: skill-history backfill manuale + dump JSON.

Uso normale: la pipeline 6h (guazza-pipeline) fa append+dump automaticamente.
Questo job serve solo per backfill manuale o re-dump.

Uso:
    guazza-skill-history append --days 30
    guazza-skill-history dump
"""

from __future__ import annotations

import duckdb
import typer
from loguru import logger

from guazza._logging import setup_logging
from guazza.jobs._common import DB_OPTION, OUTPUT_DIR_OPTION, job_run
from guazza.skill_history import (
    DEFAULT_DUMP_PATH,
    append_one,
    atomic_write_json,
    dump_payload,
)
from guazza.storage import DuckDBClient

from datetime import date, timedelta
from pathlib import Path

app = typer.Typer(help="Skill history backfill manuale + dump JSON.")


@app.callback()
def _callback() -> None:
    setup_logging()


@app.command()
def append(
    db: Path = DB_OPTION,
    day: str = typer.Option(None, help="YYYY-MM-DD; default = ieri"),
    days: int = typer.Option(1, help="Numero di giorni all'indietro (default 1 = solo ieri)"),
) -> None:
    """Upsert skill_history_daily per N giorni. Idempotente."""
    with job_run("job_skill_history_append") as stats:
        with DuckDBClient(db_path=db) as client:
            client.init_schema()
            assert client._conn is not None
            con = client._conn
            target = date.fromisoformat(day) if day else date.today() - timedelta(days=1)
            if day:
                days = 1
            total = 0
            for offset in range(days):
                d = target - timedelta(days=offset)
                n = append_one(con, d)
                logger.info(f"skill_history: {d} → {n} righe")
                total += n
        stats.rows = total
        stats.summary = f"append: {total} righe su {days} giorno/i"


@app.command()
def dump(
    db: Path = DB_OPTION,
    output: Path = typer.Option(DEFAULT_DUMP_PATH, "--output", help="Path JSON di output"),
) -> None:
    """Aggrega skill_history_daily in JSON per il frontend."""
    with job_run("job_skill_history_dump") as stats:
        con = duckdb.connect(str(db), read_only=True)
        try:
            payload = dump_payload(con)
        finally:
            con.close()
        atomic_write_json(output, payload)
        n_loc = len(payload["locations"])
        logger.info(f"skill_history.json: {n_loc} location, {payload['min_date']}→{payload['max_date']}")
        stats.rows = n_loc
        stats.summary = f"dump: {n_loc} location"


if __name__ == "__main__":
    app()
