"""The Advanced tab's native-runtime controller (ADR-005).

Lists each registered native runtime grouped by section with its on-disk install state, and
installs (off-thread venv + pip, viet-tts pulls ~GB) or uninstalls (stop sidecar + delete venv,
fast/sync) one runtime. The install/uninstall decision lives in the pure
:func:`~omnia.gui.smart_notes.html.set_native_runtime` helper; this controller injects the
off-thread runner + the page push hooks. Progress + the final outcome are pushed through
``window.__snNativeRuntime*``. Only loaded inside Anki.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Optional

from omnia.core import anki_compat
from omnia.core.logging import get_logger
from omnia.gui.smart_notes.dialogs.context import SmartNotesContext
from omnia.gui.smart_notes.html import native_runtimes_payload, set_native_runtime

logger = get_logger("smart_notes")


class NativeRuntimeController:
    """Native-runtimes panel ops (Advanced tab)."""

    def __init__(self, ctx: SmartNotesContext) -> None:
        self._ctx = ctx

    def ops(self) -> dict[str, Callable[..., Any]]:
        """The ``{op_name: handler}`` map this controller owns."""
        return {
            "native_runtimes": self.on_native_runtimes,
            "set_native_runtime": self.on_set_native_runtime,
        }

    def on_native_runtimes(self, _data: dict[str, Any]) -> dict[str, Any]:
        """The Native-runtimes panel data: each registered runtime grouped by section + state.

        Synchronous + main-thread-safe — it only reads the registry and checks each venv's
        install marker on disk (no subprocess/network). Fetched lazily when the General tab
        renders the panel.
        """
        return native_runtimes_payload(self._ctx.native_manager)

    def on_set_native_runtime(self, data: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Install (enabled=True, OFF-THREAD) or uninstall (enabled=False, sync) one runtime.

        Delegates the install/uninstall decision to the pure :func:`set_native_runtime` helper,
        injecting the off-thread runner + the page push hooks. Installing creates a venv and
        pip-installs the runtime (viet-tts pulls ~GB), so it runs off the Qt main thread via
        :func:`run_in_background` and pushes progress + the final outcome through
        ``window.__snNativeRuntime*``. Uninstalling stops the sidecar + deletes the venv (fast)
        and returns the refreshed row state immediately.
        """
        name = str(data.get("name", ""))
        try:
            return set_native_runtime(
                self._ctx.native_manager,
                name,
                bool(data.get("enabled", False)),
                run_async=self._run_native_install,
                push_progress=self._push_native_progress,
                push_done=self._push_native_done,
            )
        except (
            Exception
        ):  # boundary: a bad uninstall (rmtree) must not crash the dialog
            logger.exception("smart_notes: failed to toggle runtime %r", name)
            return {
                "name": name,
                "installed": False,
                "error": "Could not update — see logs.",
            }

    def _run_native_install(
        self,
        op: Any,
        on_success: Any,
        on_failure: Any,
    ) -> None:
        """Run a runtime install off the Qt main thread (the helper's ``run_async`` seam)."""
        anki_compat.run_in_background(
            op,
            on_success=lambda _none: on_success(),
            on_failure=on_failure,
            label="Omnia: installing native runtime…",
        )

    def _push_native_progress(self, name: str, message: str) -> None:
        """Send a runtime install progress line to ``window.__snNativeRuntimeProgress``.

        MUST marshal to the Qt main thread: this is invoked from the install op's
        ``on_progress`` callback, which runs on the ``run_in_background`` WORKER thread, and
        touching the WebView (``eval_js`` → ``webview.eval``) off the main thread hard-crashes
        Qt (a native segfault, not a catchable Python exception). ``_push_native_done`` needs no
        such marshalling — it runs from QueryOp's success/failure, already on the main thread.
        """
        anki_compat.run_on_main(
            lambda: self._ctx.eval_js(
                f"window.__snNativeRuntimeProgress({json.dumps(name)}, {json.dumps(message)});"
            )
        )

    def _push_native_done(self, name: str, installed: bool, error: str = "") -> None:
        """Send a runtime install outcome to ``window.__snNativeRuntimeDone``."""
        result: dict[str, Any] = (
            {"installed": False, "error": error} if error else {"installed": installed}
        )
        self._ctx.eval_js(
            f"window.__snNativeRuntimeDone({json.dumps(name)}, {json.dumps(result)});"
        )
