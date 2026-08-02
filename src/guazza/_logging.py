"""Shared logging setup: pretty on TTY, JSON on non-TTY (cron/pipe)."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from typing import Any

from loguru import logger


def setup_logging(level: str = "INFO") -> None:
    """Configure loguru sink based on runtime context.

    TTY (interactive): colored human-readable format on stderr.
    Non-TTY (cron, pipe): JSON structured log on stdout.

    Call only from CLI entry points, not at module level.
    """
    logger.remove()
    if sys.stderr.isatty():
        logger.add(
            sys.stderr,
            format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
            colorize=True,
            level=level,
        )
    else:
        logger.add(sys.stdout, serialize=True, level=level)


def log_scrape(scraper: str, status: str, rows: int | None = None, detail: str = "") -> None:
    """Emette un log JSON strutturato per ogni run scraper/job.

    Formato: {"scraper": ..., "status": "ok|fail", "ts": ..., "rows": N}
    """
    payload: dict[str, Any] = {
        "scraper": scraper,
        "status": status,
        "ts": datetime.now(tz=UTC).isoformat(),
    }
    if rows is not None:
        payload["rows"] = rows
    if detail:
        payload["detail"] = detail
    logger.info(payload)
