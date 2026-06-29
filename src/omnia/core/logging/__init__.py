"""Logging and observability for the add-on.

Groups the logging concerns into one package and re-exports the public surface so existing
``from omnia.core.logging import ...`` imports keep resolving:

- :mod:`~omnia.core.logging.logger` — the namespaced ``omnia`` logger (file/stderr sinks).
- :mod:`~omnia.core.logging.diagnostics` — crash logger that tees exceptions into ``omnia.log``.
- :mod:`~omnia.core.logging.session` — switchable run-capture session (timings/metrics/IO).
"""

from __future__ import annotations

from .diagnostics import install_crash_logger
from .logger import get_logger, setup_logging
from .session import (
    AsyncLoggingSession,
    LoggingSession,
    get_logging_session,
    logging_session,
)

__all__ = [
    "AsyncLoggingSession",
    "LoggingSession",
    "get_logger",
    "get_logging_session",
    "install_crash_logger",
    "logging_session",
    "setup_logging",
]
