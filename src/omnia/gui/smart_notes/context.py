"""The shared service context handed to every Smart Notes controller.

:class:`SmartNotesContext` is the single bundle of collaborators each responsibility-grouped
controller (config table, dependency graph, authoring, account, native runtimes) needs: the
page push (``eval_js``), the provider/secrets config repo, the synced per-note-type rules
store, and the native-runtime manager — plus the small shared helpers (build the provider hub,
friendly error messages, settings/deck/voice reads) that used to live on the god-class dialog.

Built ONCE by the thin :class:`~omnia.gui.smart_notes.dialog.SmartNotesDialog` shell and passed
to each controller's constructor, so the controllers stay decoupled from Qt and from each other
(the one cross-controller wire — authoring → graph — is an explicit constructor arg, not via this
context). Only loaded inside Anki; pure-logic imports stay lazy so the deps tests can stub it.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from omnia.core.logging import get_logger
from omnia.core.providers import ProviderError, ProviderHub

if TYPE_CHECKING:
    from omnia.core.config import ConfigRepository
    from omnia.core.providers.native_runtime import NativeRuntimeManager
    from omnia.plugins.smart_notes.engine import GenerationResult
    from omnia.plugins.smart_notes.integration import SmartNotesStore

logger = get_logger("smart_notes")


class SmartNotesContext:
    """Shared collaborators + helpers for the Smart Notes controllers.

    Args:
        eval_js: Pushes a JS snippet into the hosted webview (a page hook). Resolved at call
            time so the shell can build the context before ``super().__init__`` has wired the
            webview (``eval_js`` is only ever invoked from off-thread success/failure callbacks,
            long after construction).
        parent_widget: Returns the dialog widget to parent a modal file picker on (resolved at
            call time, same timing concern as ``eval_js``).
        repo: The provider/secrets config repository (llm/tts settings live here).
        store: The synced per-note-type rules store (the global option flags ride along).
        native_manager: The native-runtime sidecar manager (ADR-005), shared so install state +
            tracked servers persist across the native-runtime ops.
    """

    def __init__(
        self,
        *,
        eval_js: Callable[[str], None],
        parent_widget: Callable[[], Any],
        repo: ConfigRepository,
        store: SmartNotesStore,
        native_manager: NativeRuntimeManager,
    ) -> None:
        self.eval_js = eval_js
        self.parent_widget = parent_widget
        self.repo = repo
        self.store = store
        self.native_manager = native_manager
        # The temp clip of the last sound preview/test, replayed by the "Play again" button.
        # Lives here because result_payload (shared by preview + account test) writes it and the
        # account replay op reads it — shared state, so it sits on the shared context.
        self.last_audio_path = ""

    def settings(self) -> Any:
        """The current Smart Notes settings (per-note-type rules + global flags).

        Per-note-type rules persist in the collection (synced), not in the TOML config.
        """
        return self.store.load()

    def build_hub(self) -> Optional[ProviderHub]:
        """Build a :class:`ProviderHub` from the saved llm/tts settings, or ``None`` on error."""
        try:
            return ProviderHub(self.repo.llm_settings(), self.repo.tts_settings())
        except Exception:  # boundary: a bad provider config must not crash the dialog
            logger.exception("smart_notes: could not build provider hub for auto-smart")
            return None

    def friendly(self, exc: Exception, prefix: str) -> str:
        """A short user-facing message for ``exc`` (ProviderError verbatim; else log + generic)."""
        if isinstance(exc, ProviderError):
            return f"{prefix}: {exc}"
        logger.exception("smart_notes: %s raised", prefix)
        return f"{prefix} — see logs."

    @staticmethod
    def all_decks() -> list[dict[str, Any]]:
        """The full deck list for the picker as ``[{id, name}, ...]`` objects for the JS."""
        from omnia.core import anki_compat

        return [
            {"id": deck_id, "name": name} for deck_id, name in anki_compat.deck_names()
        ]

    @staticmethod
    def user_files_dir() -> Path:
        """The add-on's ``user_files`` directory (where the fetched-voice cache lives)."""
        from omnia import addon_user_files_dir

        return addon_user_files_dir()

    def cached_fetched_voices(self) -> dict[str, Any]:
        """The cached Refresh result merged into the baked catalog (``{}`` when absent).

        Offline-safe: when no cache exists the Auto-detect dropdowns fall back to the curated
        seed. Provider-agnostic — whatever providers were fetched + cached are merged.
        """
        from omnia.core.providers import voice_cache

        return voice_cache.load_cached_voices(self.user_files_dir())

    def result_payload(self, result: GenerationResult) -> dict[str, Any]:
        """Convert a generation result to the page payload (shared by preview + account test).

        Text carries the rendered HTML; an image is returned as a ``data:`` URI so the page can
        actually show it; audio is played here, remembered for replay (``last_audio_path``), and
        reported as a note. Nothing is inserted into a note — this is only a preview/test.
        """
        from omnia.core import anki_compat

        if result.kind == "text":
            return {"kind": "text", "text": result.text or ""}
        if result.kind == "image":
            import base64

            b64 = base64.b64encode(result.data or b"").decode("ascii")
            ext = result.ext or "png"
            return {
                "kind": "image",
                "image": f"data:image/{ext};base64,{b64}",
                "message": "Image generated.",
            }
        self.last_audio_path = anki_compat.play_audio(result.data or b"", result.ext)
        return {
            "kind": "tts",
            "message": "Audio preview played.",
            "can_replay": True,
        }
