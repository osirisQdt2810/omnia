"""Crash diagnostics: tee every displayed/uncaught exception into ``omnia.log``.

Anki shows runtime errors via a modal dialog whose "Copy Debug Info" contains only the
version + add-on list — NOT the traceback — so a user-reported crash arrives without the one
thing needed to fix it. This installs a thin wrapper around Anki's error funnel
(:func:`aqt.errors.show_exception`) plus ``sys.excepthook`` so the FULL traceback is written
to the add-on log. It never changes Anki's behaviour (the original handler still runs); it
only records. Idempotent and safe to call on every profile open.
"""

from __future__ import annotations

import contextlib
import sys
import traceback
from typing import Any


def install_crash_logger(logger: Any) -> None:
    """Wrap Anki's error funnels so every exception is also logged to ``omnia.log``.

    Wraps :func:`aqt.errors.show_exception` (the modal-dialog funnel) and chains
    ``sys.excepthook``. Both still call the original after logging, so Anki's UI is unchanged.
    No-op (logged) if ``aqt`` isn't importable. Marks itself installed to avoid double-wrapping.
    """
    try:
        import aqt.errors as aqt_errors
    except Exception:  # pragma: no cover - only hit without a running Anki
        return

    if getattr(aqt_errors, "_omnia_crash_logger_installed", False):
        return

    original_show = aqt_errors.show_exception

    def show_exception(*args: Any, **kwargs: Any) -> Any:
        exc = kwargs.get("exception")
        if exc is None and args:
            exc = args[-1]
        # never let logging break the real handler
        with contextlib.suppress(Exception):
            if isinstance(exc, BaseException):
                text = "".join(
                    traceback.format_exception(type(exc), exc, exc.__traceback__)
                )
                logger.error("Anki displayed an exception:\n%s", text)
        return original_show(*args, **kwargs)

    aqt_errors.show_exception = show_exception  # type: ignore[assignment]

    previous_hook = sys.excepthook

    def excepthook(etype: Any, value: Any, tb: Any) -> None:
        with contextlib.suppress(Exception):
            logger.error(
                "Uncaught exception:\n%s",
                "".join(traceback.format_exception(etype, value, tb)),
            )
        previous_hook(etype, value, tb)

    sys.excepthook = excepthook
    aqt_errors._omnia_crash_logger_installed = True  # type: ignore[attr-defined]
    logger.info("crash logger installed (tracebacks will be written here)")
