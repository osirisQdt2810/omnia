"""Add-on logging.

A single namespaced logger (``omnia``) writing to the add-on's ``user_files`` directory
when running inside Anki, or to stderr otherwise. Pure module — safe to import headless.

IMPORTANT: inside Anki we must NOT write to ``stderr`` and must NOT propagate to the root
logger. Anki installs an ``ErrorHandler`` that hijacks ``sys.stderr`` and treats anything
written there as a crash — popping the "problem may be caused by an add-on" dialog. Because
logging is configured at profile-open (after that hijack), a plain ``StreamHandler`` would
funnel every INFO line into that dialog. So: file handler only when we have a log dir, the
stderr handler is a headless/test fallback, and ``propagate`` is disabled.
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
    # Never let Omnia's records bubble to the root logger: Anki configures a root handler on
    # the hijacked stderr, so propagation would feed our logs into Anki's crash dialog.
    logger.propagate = False
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    file_added = False
    if log_dir is not None:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(log_dir / "omnia.log", encoding="utf-8")
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
            file_added = True
        except OSError:
            # A read-only or missing log dir must never stop the add-on from loading.
            pass

    # Fall back to stderr ONLY when there's no file sink (headless/tests). Writing to stderr
    # inside Anki triggers its ErrorHandler "add-on problem" dialog — see the module docstring.
    if not file_added:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        logger.addHandler(stream)

    return logger


def get_logger(plugin_id: Optional[str] = None) -> logging.Logger:
    """Return the Omnia logger, optionally scoped to a plugin (``omnia.<plugin_id>``)."""
    if plugin_id:
        return logging.getLogger(f"{_LOGGER_NAME}.{plugin_id}")
    return logging.getLogger(_LOGGER_NAME)
