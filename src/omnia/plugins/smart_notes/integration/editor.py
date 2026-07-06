"""Editor glue for smart_notes: the ✨ button that generates fields for the open note.

Thin Anki glue around the plugin's shared generation core. The button is registered on the
``editor_did_init_buttons`` hook with a stable id + the ``Ctrl+Shift+G`` shortcut (mirroring
the reference add-on); clicking it hands the current editor to the supplied ``on_click``
callback (the plugin), which builds the plan and runs generation off-thread. While a
background run is in flight the button is greyed out via the editor webview, then re-enabled
on success or failure.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

_BUTTON_LABEL = "✨"
_BUTTON_TIP = "[Omnia] Generate Smart Fields — Ctrl+Shift+G"
_BUTTON_CMD = "omnia_smart_notes"
# Stable DOM id so the in-flight greying JS can target exactly this button.
_BUTTON_ID = "generate_smart_fields"
_SHORTCUT = "Ctrl+Shift+G"


def add_generate_button(
    buttons: list[Any], editor: Any, on_click: Callable[[Any], None]
) -> None:
    """Append the ✨ generate-fields button to the editor's button row.

    Args:
        buttons: The editor's button HTML list to append to (from the hook).
        editor: The Anki ``Editor`` whose note the button operates on.
        on_click: Called with the ``editor`` when the button is clicked; Anki has already
            saved the open note by then (``addButton`` wraps the handler in
            ``call_after_note_saved``), so ``editor.note`` is current.
    """
    button = editor.addButton(
        icon=None,
        cmd=_BUTTON_CMD,
        func=lambda ed: on_click(ed),
        tip=_BUTTON_TIP,
        label=_BUTTON_LABEL,
        id=_BUTTON_ID,
        keys=_SHORTCUT,
    )
    buttons.append(button)


def set_button_enabled(editor: Any, enabled: bool) -> None:
    """Grey out (or restore) the ✨ button in the editor webview during a background run.

    Anki exposes no Python API to toggle a top editor button, so — like the reference add-on —
    we imperatively set the DOM element's ``disabled``/``opacity`` by its id. Safe no-op if the
    editor's webview is gone (the note was closed mid-generation).
    """
    web = getattr(editor, "web", None)
    if web is None:
        return
    opacity = "1.0" if enabled else "0.25"
    disabled = "false" if enabled else "true"
    web.eval(f"""
        (() => {{
            const button = document.querySelector("#{_BUTTON_ID}");
            if (button) {{
                button.disabled = {disabled};
                button.style.opacity = {opacity};
            }}
        }})()
        """)
