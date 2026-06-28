"""Editor glue for smart_notes: the ✨ button that generates fields for the open note.

Thin Anki glue around the plugin's shared generation core. The button is registered on the
``editor_did_init_buttons`` hook; clicking it hands the current editor to the supplied
``on_click`` callback (the plugin), which builds the plan and runs generation off-thread.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

_BUTTON_LABEL = "✨"
_BUTTON_TIP = "Omnia: generate fields"
_BUTTON_CMD = "omnia_smart_notes"


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
    )
    buttons.append(button)
