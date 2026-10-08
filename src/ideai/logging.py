"""Logging strutturato (§11.3): JSON in produzione, console in sviluppo.

Ogni riga porta con sé i campi di contesto ``job_id``, ``idea_id``, ``source_kind``
e ``model_spec`` grazie a ``structlog.contextvars``.
"""

from __future__ import annotations

import logging
import sys

import structlog

CONTEXT_FIELDS = ("job_id", "idea_id", "source_kind", "model_spec")


def configure_logging(settings) -> None:
    """Configura structlog + stdlib logging con un unico formatter."""
    level = getattr(logging, str(settings.log_level).upper(), logging.INFO)

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)
    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        timestamper,
    ]

    if settings.env == "prod":
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    for noisy in ("httpx", "httpcore", "asyncpraw", "asyncprawcore"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))


def get_logger(name: str = "ideai"):
    return structlog.get_logger(name)
