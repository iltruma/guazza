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
from pathlib import Path

from dotenv import load_dotenv

# Carica .env prima che i moduli guazza leggano le env a import time (es. DB_PATH
# in guazza._paths). In k8s non ha effetto (le env sono già iniettate dal pod spec).
load_dotenv(Path(__file__).resolve().parents[4] / ".env")

import httpx  # noqa: E402
import typer  # noqa: E402
from loguru import logger  # noqa: E402

from guazza._logging import log_scrape  # noqa: E402
from guazza._paths import DEFAULT_CONFIG_DIR, DEFAULT_DB_PATH, DEFAULT_OUTPUT_DIR  # noqa: E402

DB_OPTION = typer.Option(DEFAULT_DB_PATH, "--db", help="Path file DuckDB")
CONFIG_DIR_OPTION = typer.Option(
    DEFAULT_CONFIG_DIR, "--config-dir", help="Directory YAML config"
)
OUTPUT_DIR_OPTION = typer.Option(
    DEFAULT_OUTPUT_DIR, "--output-dir", help="Directory output JSON"
)


def _resolve_healthchecks_url(job_name: str) -> str:
    """Risolve l'URL Healthchecks.io per il job: prima HEALTHCHECKS_URL_{JOB_NAME_UPPER},
    poi HEALTHCHECKS_URL come fallback. Restituisce stringa vuota se nessuna è configurata.
    """
    specific = os.environ.get(f"HEALTHCHECKS_URL_{job_name.upper()}", "").strip()
    if specific:
        return specific
    return os.environ.get("HEALTHCHECKS_URL", "").strip()


def ping_healthchecks(status: str = "", base_url: str = "") -> None:
    """Invia ping a Healthchecks.io se l'URL è configurato (altrimenti skip).

    status: "" = ok, "/fail" = fail, "/start" = start.
    base_url: se vuoto, legge HEALTHCHECKS_URL dall'env (backward-compatible).
    """
    resolved = base_url.strip() or os.environ.get("HEALTHCHECKS_URL", "").strip()
    if not resolved:
        return
    url = resolved.rstrip("/") + status
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

    L'URL Healthchecks.io viene risolto con HEALTHCHECKS_URL_{JOB_NAME_UPPER} come
    primo candidato, con fallback a HEALTHCHECKS_URL.
    """
    hc_url = _resolve_healthchecks_url(job_name)
    ping_healthchecks("/start", base_url=hc_url)
    t0 = time.monotonic()
    stats = JobStats()
    try:
        yield stats
    except typer.Exit:
        raise
    except Exception as e:
        logger.exception(f"{job_name} fallito: {e}")
        ping_healthchecks("/fail", base_url=hc_url)
        raise typer.Exit(1) from e
    elapsed = time.monotonic() - t0
    log_scrape(job_name, "ok", rows=stats.rows)
    ping_healthchecks(base_url=hc_url)
    suffix = f" — {stats.summary}" if stats.summary else ""
    typer.echo(f"{job_name} completato in {elapsed:.0f}s{suffix}")
