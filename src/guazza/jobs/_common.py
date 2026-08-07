"""Helper condivisi dai job CLI cron: push Uptime Kuma, ciclo di vita, opzioni typer.

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


def filter_locations(
    locations_all: dict[str, object],
    requested: list[str] | None,
) -> dict[str, object]:
    """Filtra locations_all alle sole location richieste, con validazione.

    Se requested è None o vuoto, restituisce tutte le location invariate.
    Esce con un messaggio d'errore se una location richiesta non esiste.
    """
    if not requested:
        return locations_all
    unknown = set(requested) - set(locations_all)
    if unknown:
        typer.echo(f"Errore: location sconosciute: {sorted(unknown)}")
        typer.echo(f"Disponibili: {list(locations_all.keys())}")
        raise typer.Exit(1)
    return {k: v for k, v in locations_all.items() if k in requested}


DB_OPTION = typer.Option(DEFAULT_DB_PATH, "--db", help="Path file DuckDB")
CONFIG_DIR_OPTION = typer.Option(
    DEFAULT_CONFIG_DIR, "--config-dir", help="Directory YAML config"
)
OUTPUT_DIR_OPTION = typer.Option(
    DEFAULT_OUTPUT_DIR, "--output-dir", help="Directory output JSON"
)


def _resolve_push_url(job_name: str) -> str:
    """Risolve l'URL push Uptime Kuma per il job: prima KUMA_PUSH_URL_{JOB_NAME_UPPER},
    poi KUMA_PUSH_URL come fallback. Restituisce stringa vuota se nessuna è configurata.
    """
    specific = os.environ.get(f"KUMA_PUSH_URL_{job_name.upper()}", "").strip()
    if specific:
        return specific
    return os.environ.get("KUMA_PUSH_URL", "").strip()


def ping_monitor_alert() -> None:
    """Invia push a KUMA_PUSH_URL_MONITOR se configurata, altrimenti solo warning.

    Canale separato dal check principale del job: il drift ACI non è un
    fallimento del job, quindi non deve sovrascrivere il push up finale.
    """
    url = os.environ.get("KUMA_PUSH_URL_MONITOR", "").strip()
    if not url:
        logger.warning("KUMA_PUSH_URL_MONITOR non configurata — drift ACI non notificato")
        return
    try:
        httpx.get(url, params={"status": "down", "msg": "drift ACI"}, timeout=5)
        logger.info(f"Push Uptime Kuma inviato: {url}?status=down (drift ACI)")
    except Exception as e:
        logger.warning(f"Push Uptime Kuma fallito: {e}")


def push_job_status(job_name: str, ok: bool, base_url: str = "") -> None:
    """Invia il push di stato a Uptime Kuma se l'URL è configurato (altrimenti skip).

    ok: True = status=up, False = status=down.
    base_url: se vuoto, legge KUMA_PUSH_URL dall'env (backward-compatible).
    """
    resolved = base_url.strip() or os.environ.get("KUMA_PUSH_URL", "").strip()
    if not resolved:
        return
    try:
        httpx.get(
            resolved.rstrip("/"),
            params={"status": "up" if ok else "down", "msg": job_name},
            timeout=5,
        )
        logger.debug(f"Push Uptime Kuma: {job_name} {'up' if ok else 'down'}")
    except Exception as e:
        logger.warning(f"Push Uptime Kuma fallito: {e}")


@dataclass
class JobStats:
    """Riepilogo del job, compilato dal chiamante dentro `with job_run(...)`."""

    rows: int = 0
    summary: str = ""


@contextmanager
def job_run(job_name: str) -> Generator[JobStats]:
    """Ciclo di vita standard di un job cron.

    Ingresso: nessun ping (Uptime Kuma è interval-based, il push arriva a fine run).
    Uscita ok: log_scrape(job_name, "ok", rows), push status=up, echo del riepilogo
    con tempo trascorso. Eccezione: log ERROR con traceback, push status=down, exit 1.

    L'URL Uptime Kuma viene risolto con KUMA_PUSH_URL_{JOB_NAME_UPPER} come
    primo candidato, con fallback a KUMA_PUSH_URL.
    """
    push_url = _resolve_push_url(job_name)
    t0 = time.monotonic()
    stats = JobStats()
    try:
        yield stats
    except typer.Exit:
        raise
    except Exception as e:
        logger.exception(f"{job_name} fallito: {e}")
        push_job_status(job_name, ok=False, base_url=push_url)
        raise typer.Exit(1) from e
    elapsed = time.monotonic() - t0
    log_scrape(job_name, "ok", rows=stats.rows)
    push_job_status(job_name, ok=True, base_url=push_url)
    suffix = f" — {stats.summary}" if stats.summary else ""
    typer.echo(f"{job_name} completato in {elapsed:.0f}s{suffix}")
