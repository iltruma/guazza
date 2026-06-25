"""Helper condivisi dai job CLI cron: ping Healthchecks, ciclo di vita, opzioni typer.

Ogni job segue lo stesso ciclo: ping /start, lavoro, log_scrape finale + ping ok
oppure log ERROR + ping /fail + exit 1. `job_run` lo incapsula una volta sola.
"""

from __future__ import annotations

import os
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass

import httpx
import typer
from loguru import logger

from guazza._logging import log_scrape
from guazza._paths import DEFAULT_CONFIG_DIR, DEFAULT_DB_PATH, DEFAULT_OUTPUT_DIR

DB_OPTION = typer.Option(DEFAULT_DB_PATH, "--db", help="Path file DuckDB")
CONFIG_DIR_OPTION = typer.Option(
    DEFAULT_CONFIG_DIR, "--config-dir", help="Directory YAML config"
)
OUTPUT_DIR_OPTION = typer.Option(
    DEFAULT_OUTPUT_DIR, "--output-dir", help="Directory output JSON"
)


def ping_healthchecks(status: str = "") -> None:
    """Invia ping a Healthchecks.io se HEALTHCHECKS_URL è configurato (altrimenti skip).

    status: "" = ok, "/fail" = fail, "/start" = start.
    """
    base_url = os.environ.get("HEALTHCHECKS_URL", "").strip()
    if not base_url:
        return
    url = base_url.rstrip("/") + status
    try:
        httpx.get(url, timeout=5)
        logger.debug(f"Healthchecks ping: {url}")
    except Exception as e:
        logger.warning(f"Healthchecks ping fallito: {e}")


@dataclass
class JobStats:
    """Riepilogo del job, compilato dal chiamante dentro `with job_run(...)`."""

    rows: int = 0
    summary: str = ""


@contextmanager
def job_run(job_name: str) -> Generator[JobStats]:
    """Ciclo di vita standard di un job cron.

    Ingresso: ping /start. Uscita ok: log_scrape(job_name, "ok", rows), ping ok,
    echo del riepilogo con tempo trascorso. Eccezione: log ERROR con traceback,
    ping /fail, exit code 1.
    """
    ping_healthchecks("/start")
    t0 = time.monotonic()
    stats = JobStats()
    try:
        yield stats
    except typer.Exit:
        raise
    except Exception as e:
        logger.exception(f"{job_name} fallito: {e}")
        ping_healthchecks("/fail")
        raise typer.Exit(1) from e
    elapsed = time.monotonic() - t0
    log_scrape(job_name, "ok", rows=stats.rows)
    ping_healthchecks()
    suffix = f" — {stats.summary}" if stats.summary else ""
    typer.echo(f"{job_name} completato in {elapsed:.0f}s{suffix}")
