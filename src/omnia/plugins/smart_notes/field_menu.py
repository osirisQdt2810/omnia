"""The editor field right-click context menu for smart_notes (ports the reference field_menu).

Adds, on the field the cursor is in: "✨ Generate this field" (runs that field's rule(s) on
demand, even when disabled for batching) plus 💬/🔈/🖼️ one-off custom-prompt palettes that
generate into the field without saving a rule. Thin Anki glue; the per-field selection logic
lives in ``logic.py`` and the palettes in ``gui/smart_notes_custom_prompt``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Optional

from omnia.core import anki_compat

if TYPE_CHECKING:
    from omnia.core.config.models import SmartNotesNoteTypeConfig
    from omnia.core.plugin import PluginContext

_KINDS = ("text", "tts", "image")
_CUSTOM_LABELS = {
    "text": "💬 Custom Text",
    "tts": "🔈 Custom TTS",
    "image": "🖼️ Custom Image",
}


def build_field_menu(
    ctx: Optional[PluginContext],
    editor: Any,
    menu: Any,
    generate_field: Callable[[Any, str], None],
) -> None:
    """Attach the smart_notes actions for the editor's current field to ``menu``.

    Args:
        ctx: The plugin context (for the config repo the custom palettes read providers from).
        editor: The Anki ``Editor`` the menu belongs to.
        menu: The ``QMenu`` Anki is about to show.
        generate_field: Called as ``generate_field(editor, field_name)`` to run that field's
            configured rule(s) on demand.
    """
    from aqt.qt import QAction

    note = getattr(editor, "note", None)
    field = _current_field_name(editor, note)
    if note is None or not field or ctx is None:
        return

    menu.addSeparator()
    generate = QAction("✨ Generate this field", menu)
    generate.triggered.connect(lambda: generate_field(editor, field))
    menu.addAction(generate)

    note_type = _note_type_name(note)
    field_names = list(note.keys())
    for kind in _KINDS:
        action = QAction(_CUSTOM_LABELS[kind], menu)
        action.triggered.connect(
            lambda _c=False, k=kind: _open_custom_prompt(
                ctx, editor, note, note_type, field_names, field, k
            )
        )
        menu.addAction(action)


def single_field_config(
    config: Optional[SmartNotesNoteTypeConfig], field: str
) -> Optional[SmartNotesNoteTypeConfig]:
    """Return a one-field copy of ``config`` for the on-demand "generate this field" action.

    The field is forced ``enabled`` (the menu generates it even when it's disabled for batch)
    and its target excluded if it is the base field. Returns None when ``config`` has no row
    for ``field`` (or it IS the base field), so the caller can tell the user there's nothing
    to generate.
    """
    if config is None or field == config.base_field:
        return None
    for row in config.fields:
        if row.field == field:
            return config.copy(update={"fields": [row.copy(update={"enabled": True})]})
    return None


def _open_custom_prompt(
    ctx: PluginContext,
    editor: Any,
    note: Any,
    note_type: str,
    field_names: list[str],
    field: str,
    kind: str,
) -> None:
    from omnia.gui.smart_notes_custom_prompt import CustomPromptDialog

    def on_save(value: str) -> None:
        if field not in note:
            return
        note[field] = value
        if getattr(note, "id", 0):
            anki_compat.update_note(note)
        for attr in ("loadNoteKeepingFocus", "loadNote"):
            reload_view = getattr(editor, attr, None)
            if callable(reload_view):
                reload_view()
                break

    CustomPromptDialog(
        ctx.config,
        kind=kind,
        note_type=note_type,
        field_names=field_names,
        target_field=field,
        on_save=on_save,
        parent=getattr(editor, "parentWindow", None),
    ).exec()


def _current_field_name(editor: Any, note: Any) -> Optional[str]:
    """Return the name of the field the cursor is in, or None.

    ``editor.currentField`` is the field index; map it through the note's field names.
    """
    if note is None:
        return None
    index = getattr(editor, "currentField", None)
    names = list(note.keys())
    if index is None or not isinstance(index, int) or not (0 <= index < len(names)):
        return None
    return names[index]


def _note_type_name(note: Any) -> str:
    """Return the note's note-type name across Anki versions (``note_type`` / ``model``)."""
    for attr in ("note_type", "model"):
        getter = getattr(note, attr, None)
        if callable(getter):
            data = getter()
            if isinstance(data, dict):
                return str(data.get("name", ""))
    return ""
