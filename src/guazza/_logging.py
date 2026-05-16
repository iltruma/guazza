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
    if sys.stderr.isatty():
        logger.add(
            sys.stderr,
            format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
            colorize=True,
            level=level,
        )
    else:
        logger.add(sys.stdout, serialize=True, level=level)
