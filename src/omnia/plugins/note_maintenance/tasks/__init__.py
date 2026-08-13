"""The bundled maintenance tasks.

Importing this package must register every task (the ``@register_task`` decorator runs at
import). Add a line per task below as it is implemented; keep imports side-effect-only (class
definitions, no Anki work at import time).
"""

from __future__ import annotations

# Registered tasks (each import runs its @register_task decorator). F401 "unused import" is
# expected here — these imports exist purely for their registration side effect, and the
# pyproject per-file-ignore for __init__.py already allows it.
from omnia.plugins.note_maintenance.tasks.extract_audio_file_name import (
    ExtractAudioFileNameTask,
)
from omnia.plugins.note_maintenance.tasks.fill_first_example import FillFirstExampleTask
from omnia.plugins.note_maintenance.tasks.reformat_synonyms import ReformatSynonymsTask
from omnia.plugins.note_maintenance.tasks.replace_text_all_fields import (
    ReplaceTextAllFieldsTask,
)
from omnia.plugins.note_maintenance.tasks.strip_ipa import StripIpaTask
