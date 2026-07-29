"""Structured logging configuration. Stdlib-based, idempotent.

- Human-readable text on a TTY (local dev).
- Single-line JSON on non-TTY stdout (containerized / Azure Container Apps).
"""
import logging
import sys

_CONFIGURED = False


def configure_logging(level: str = "INFO") -> None:
    """Configure root logging. Safe to call multiple times — only the first wins."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    root = logging.getLogger()
    root.setLevel(level.upper())

    # Drop any pre-existing handlers (e.g. from uvicorn's preload).
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stdout)

    if sys.stdout.isatty():
        fmt = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
        datefmt = "%Y-%m-%d %H:%M:%S"
    else:
        # Container-friendly JSON line. Swap for structlog in Stage 2+ if needed.
        fmt = (
            '{"ts":"%(asctime)s","level":"%(levelname)s",'
            '"logger":"%(name)s","msg":"%(message)s"}'
        )
        datefmt = "%Y-%m-%dT%H:%M:%S%z"

    handler.setFormatter(logging.Formatter(fmt=fmt, datefmt=datefmt))
    root.addHandler(handler)

    # Tame noisy libs.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Thin convenience wrapper."""
    return logging.getLogger(name)
