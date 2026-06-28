"""Collection-backed persistence for the smart_notes per-note-type rules.

The per-note-type :class:`~omnia.plugins.smart_notes.config.SmartNotesSettings` lives in the
Anki COLLECTION (``col.get_config``/``col.set_config``) rather than the add-on's TOML config,
so the rules sync across devices. Provider config (``[llm]``/``[tts]``) STAYS in the TOML
config via the ``ConfigRepository`` — only the note-type rules move into the collection.

The collection is resolved LAZILY: ``mw.col`` is not ready at add-on init time, so the store
fetches it per call. A missing collection (e.g. headless, or before Anki finishes loading)
degrades to a default empty :class:`SmartNotesSettings` on load and a no-op on save.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from omnia.plugins.smart_notes.config import SmartNotesSettings


class SmartNotesStore:
    """Persists :class:`SmartNotesSettings` in the collection config (synced across devices).

    ``col`` is resolved lazily (``mw.col`` isn't ready at add-on init); an optional
    ``col_provider`` lets tests inject a fake collection.
    """

    KEY = "omnia:smart_notes"

    def __init__(self, col_provider: Optional[Callable[[], Any]] = None) -> None:
        self._col_provider = col_provider

    def _col(self) -> Any:
        if self._col_provider is not None:
            try:
                return self._col_provider()
            except Exception:
                return None
        from omnia.core import anki_compat

        try:
            return anki_compat.main_window().col
        except Exception:
            return None

    def load(self) -> SmartNotesSettings:
        """Return the persisted settings, or a default empty one when no collection/config."""
        from omnia.plugins.smart_notes.config import SmartNotesSettings

        col = self._col()
        raw = col.get_config(self.KEY, None) if col is not None else None
        return SmartNotesSettings.parse_obj(raw or {})

    def save(self, settings: SmartNotesSettings) -> None:
        """Persist ``settings`` into the collection config (no-op without a collection)."""
        col = self._col()
        if col is not None:
            col.set_config(self.KEY, settings.dict())
