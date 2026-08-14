"""Copy the bare filename out of an Anki ``[sound:…]`` reference into a plain-text field.

``[sound:plunge.mp3]`` → ``plunge.mp3``. Card templates and external tooling often need the
filename as text (to build a link, or to check the media folder) without the tag that makes
Anki play it.

A media reference holds a FILE NAME, not markup — Anki decodes HTML entities when it resolves
one, so ``[sound:rock &amp; roll.mp3]`` plays ``rock & roll.mp3``. The name is therefore decoded
and re-encoded through :func:`~omnia.core.lang.text.as_field_html` on its way into the target field:
copying the raw bytes across would leave a hand-written ``&`` sitting in stored HTML as the start
of an entity.

Pure module: no ``aqt``/``anki`` imports.
"""

from __future__ import annotations

import html
import re

from pydantic import Field

from omnia.core.lang.text import as_field_html
from omnia.plugins.note_maintenance.base import (
    MaintenanceTask,
    NoteView,
    OptionKind,
    TaskConfigBase,
)
from omnia.plugins.note_maintenance.registry import register_task

# A field holding EXACTLY one sound reference (nothing else) — anything richer is left alone.
_SOUND_TAG_RE = re.compile(r"^\s*\[sound:([^\]]+)\]\s*$", re.IGNORECASE)


class ExtractAudioFileNameConfig(TaskConfigBase):
    """Which audio fields to read, and where to put their filenames."""

    order: int = Field(
        30,
        title="Run order",
        description="Independent of the synonym tasks; runs after them by convention.",
    )
    fields: dict[str, str] = Field(
        default_factory=lambda: {
            "Dictionary Definition Audio": "Dictionary Definition AudioNoTag",
            "First Example Audio": "First Example AudioNoTag",
        },
        title="Audio fields to read",
        description=(
            "``{audio field: filename field}``. The filename is written to the target field; "
            "an EMPTY target replaces the sound tag in the source field itself."
        ),
        renders_as=OptionKind.FIELD_MAP,
    )


@register_task("extract_audio_file_name")
class ExtractAudioFileNameTask(MaintenanceTask):
    """Writes the filename of a field's ``[sound:…]`` reference to a plain-text field."""

    name = "Extract audio file name"
    description = "Copy the file name out of a [sound:…] reference into a text field."
    config_model = ExtractAudioFileNameConfig

    def process(self, note: NoteView) -> dict[str, str]:
        updates: dict[str, str] = {}
        for source, target in self.config.fields.items():
            match = _SOUND_TAG_RE.match(note.field(source).strip())
            filename = (match.group(1) or "").strip() if match else ""
            if not filename:
                continue
            # html.unescape first: the reference is stored HTML, and the file name Anki plays
            # is its DECODED form — re-encoding that is what the target field has to hold.
            written = as_field_html(html.unescape(filename))
            destination = target or source
            if note.field(destination) != written:
                updates[destination] = written
        return updates
