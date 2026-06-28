"""Feature plugins.

Importing this package must register every feature with the registry (the ``@register``
decorator runs at import). Add a line per feature below as it is implemented; keep imports
side-effect-only (class definitions, no Anki work at import time).
"""

from __future__ import annotations

# Registered features (each import runs its @register decorator). F401 "unused import" is
# expected here — these imports exist purely for their registration side effect, and the
# pyproject per-file-ignore for __init__.py already allows it.
from omnia.plugins.auto_flip import AutoFlipPlugin
from omnia.plugins.display_interval import DisplayIntervalPlugin
from omnia.plugins.overdue_guard import OverdueGuardPlugin
from omnia.plugins.smart_notes import SmartNotesPlugin
from omnia.plugins.typed_accuracy import TypedAccuracyPlugin
