"""Data models for prompt authoring (auto-smart suggestions).

Pure module — no Anki imports.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AutoSmartField:
    """The LLM's suggestion for one field: its generation ``type`` + ``prompt`` template."""

    type: str
    prompt: str
