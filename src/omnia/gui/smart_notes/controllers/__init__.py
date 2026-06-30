"""Responsibility-grouped controllers for the Smart Notes dialog.

Each controller takes the shared :class:`~omnia.gui.smart_notes.context.SmartNotesContext` and
exposes an ``ops() -> {op_name: handler}`` map the thin shell assembles into the ``pycmd``
handler dict. Composition over a god-class: the shell wires them together; the only
cross-controller dependency is authoring → graph (the prompt→graph classify fold).
"""

from __future__ import annotations

from omnia.gui.smart_notes.controllers.account import AccountController
from omnia.gui.smart_notes.controllers.authoring import AuthoringController
from omnia.gui.smart_notes.controllers.config import ConfigController
from omnia.gui.smart_notes.controllers.graph import GraphController
from omnia.gui.smart_notes.controllers.native_runtime import NativeRuntimeController

__all__ = [
    "AccountController",
    "AuthoringController",
    "ConfigController",
    "GraphController",
    "NativeRuntimeController",
]
