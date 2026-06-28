"""Add-on logging.

A single namespaced logger (``omnia``) writing to the add-on's ``user_files`` directory
when running inside Anki, or to stderr otherwise. Pure module — safe to import headless.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

_LOGGER_NAME = "omnia"


def setup_logging(
    log_dir: Optional[Path] = None, level: int = logging.INFO
) -> logging.Logger:
    """Configure and return the root Omnia logger (idempotent).

    Args:
        log_dir: Directory for ``omnia.log``. If None, logs go to stderr only.
        level: Logging level.

    Returns:
        The configured ``omnia`` logger.
    """
    logger = logging.getLogger(_LOGGER_NAME)
    if logger.handlers:  # already configured (idempotent, no module-level state)
        return logger

    logger.setLevel(level)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    logger.addHandler(stream)

    if log_dir is not None:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(log_dir / "omnia.log", encoding="utf-8")
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        except OSError:
            # A read-only or missing log dir must never stop the add-on from loading.
            logger.warning(
                "Could not open log file in %s; logging to stderr only", log_dir
            )

    return logger


def get_logger(plugin_id: Optional[str] = None) -> logging.Logger:
    """Return the Omnia logger, optionally scoped to a plugin (``omnia.<plugin_id>``)."""
    if plugin_id:
        return logging.getLogger(f"{_LOGGER_NAME}.{plugin_id}")
    return logging.getLogger(_LOGGER_NAME)
