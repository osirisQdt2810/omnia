"""Tests for the crash-logger diagnostic (tees Anki exceptions into omnia.log)."""

from __future__ import annotations

import sys
import types

import pytest

from omnia.core.diagnostics import install_crash_logger


@pytest.fixture
def fake_aqt_errors(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """Install a fake ``aqt.errors`` module with a recording ``show_exception`` funnel."""
    errors = types.ModuleType("aqt.errors")
    errors.calls = []  # type: ignore[attr-defined]

    def show_exception(*, parent=None, exception=None):  # type: ignore[no-untyped-def]
        errors.calls.append(exception)  # type: ignore[attr-defined]

    errors.show_exception = show_exception  # type: ignore[attr-defined]

    class ErrorHandler:
        """Mirror of Anki's stderr-buffering error handler (pool + onTimeout)."""

        def __init__(self) -> None:
            self.pool = ""
            self.timed_out = False

        def onTimeout(self):  # mirrors Anki's method name
            self.timed_out = True
            self.pool = ""

    errors.ErrorHandler = ErrorHandler  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "aqt", types.ModuleType("aqt"))
    monkeypatch.setitem(sys.modules, "aqt.errors", errors)
    return errors


class _ListLogger:
    """A minimal logger capturing ``error``/``info`` calls as formatted strings."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def error(self, msg: str, *args: object) -> None:
        self.messages.append(msg % args if args else msg)

    def info(self, msg: str, *args: object) -> None:
        self.messages.append(msg % args if args else msg)


class TestInstallCrashLogger:
    def test_logs_the_full_traceback_and_still_shows_the_dialog(
        self, fake_aqt_errors: types.ModuleType
    ) -> None:
        logger = _ListLogger()
        install_crash_logger(logger)

        try:
            raise ValueError("boom-in-deck-browser")
        except ValueError as exc:
            fake_aqt_errors.show_exception(parent=None, exception=exc)

        joined = "\n".join(logger.messages)
        assert "boom-in-deck-browser" in joined
        assert "ValueError" in joined
        assert "Traceback" in joined
        # The original handler still ran (Anki's UI is unchanged).
        assert len(fake_aqt_errors.calls) == 1  # type: ignore[attr-defined]

    def test_is_idempotent(self, fake_aqt_errors: types.ModuleType) -> None:
        first = fake_aqt_errors.show_exception
        install_crash_logger(_ListLogger())
        wrapped_once = fake_aqt_errors.show_exception
        install_crash_logger(_ListLogger())
        # Second install must NOT wrap again (no stacking of wrappers).
        assert fake_aqt_errors.show_exception is wrapped_once
        assert wrapped_once is not first

    def test_excepthook_is_chained(self, fake_aqt_errors: types.ModuleType) -> None:
        logger = _ListLogger()
        prior_called: list[bool] = []
        sys.excepthook = lambda *a: prior_called.append(True)
        try:
            install_crash_logger(logger)
            try:
                raise RuntimeError("uncaught-boom")
            except RuntimeError:
                sys.excepthook(*sys.exc_info())
            assert any("uncaught-boom" in m for m in logger.messages)
            assert prior_called == [True]  # the previous hook still ran
        finally:
            sys.excepthook = sys.__excepthook__

    def test_errorhandler_pool_is_logged(
        self, fake_aqt_errors: types.ModuleType
    ) -> None:
        # Anki's generic add-on error dialog goes through ErrorHandler.onTimeout, NOT
        # show_exception — the crash logger must capture its accumulated traceback pool.
        logger = _ListLogger()
        install_crash_logger(logger)
        handler = fake_aqt_errors.ErrorHandler()  # type: ignore[attr-defined]
        handler.pool = 'Traceback (most recent call last):\n  ...\nKeyError: "boom"'
        handler.onTimeout()
        joined = "\n".join(logger.messages)
        assert "Anki error handler captured" in joined
        assert 'KeyError: "boom"' in joined
        assert handler.timed_out  # original onTimeout still ran

    def test_no_aqt_is_a_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Simulate a fully headless environment with no aqt importable.
        monkeypatch.setitem(sys.modules, "aqt.errors", None)
        install_crash_logger(_ListLogger())  # must not raise
