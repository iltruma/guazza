"""Shared logging setup: pretty on TTY, JSON on non-TTY (cron/pipe)."""

from __future__ import annotations

import sys

from loguru import logger


def setup_logging(level: str = "INFO") -> None:
    """Configure loguru sink based on runtime context.

    TTY (interactive): colored human-readable format on stderr.
    Non-TTY (cron, pipe): JSON structured log on stdout.

    Call only from CLI entry points, not at module level.
    """
    logger.remove()
    sink, colorize = (sys.stderr, True) if sys.stderr.isatty() else (sys.stdout, False)
    logger.add(
        sink,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
        colorize=colorize,
        level=level,
    )


def log_scrape(scraper: str, status: str, rows: int | None = None, detail: str = "") -> None:
    """Emits a structured log event for each scraper/job run.

    Fields land in record.extra (JSON sink) or inline message (TTY).
    Format: scraper=<src>:<id> status=ok|fail [rows=N] [detail=...]
    """
    bound = logger.bind(scraper=scraper, status=status)
    if rows is not None:
        bound = bound.bind(rows=rows)
    message = f"scrape {scraper} {status}" + (f" rows={rows}" if rows is not None else "")
    if detail:
        bound = bound.bind(detail=detail)
        message += f" detail={detail}"
    bound.info(message)
