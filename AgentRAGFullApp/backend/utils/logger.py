"""Structured logging configuration."""

from __future__ import annotations

import logging
import sys


def setup_logger(
    name: str = "agent-rag",
    level: str = "INFO",
    fmt: str = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
) -> logging.Logger:
    """Configure root + named logger so submodule loggers also emit."""
    # Configure root so logger.getLogger(__name__) calls also propagate
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(fmt, datefmt="%Y-%m-%d %H:%M:%S"))
        root.addHandler(handler)

    # Quiet down noisy libraries
    for noisy in ("httpx", "httpcore", "openai", "asyncio", "watchfiles", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    return logger


# Default application logger
app_logger = setup_logger()
