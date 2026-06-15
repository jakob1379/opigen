from __future__ import annotations

import logging
import sys

import structlog


def configure_logging(level: str = "info", format: str = "json") -> None:
    level_name = level.upper()
    level_number = logging.getLevelName(level_name)
    if not isinstance(level_number, int):
        level_name = "INFO"
        level_number = logging.INFO

    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    if format == "console":
        processors.append(structlog.dev.ConsoleRenderer(colors=False))
    else:
        processors.append(structlog.processors.JSONRenderer(sort_keys=True))

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level_number, force=True)
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level_number),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=False,
    )
    logging.getLogger().setLevel(level_name)
