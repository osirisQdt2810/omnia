"""Data models for prompt authoring (auto-smart suggestions).

Pure module — no Anki imports.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AutoSmartDep:
    """The LLM's suggested dependency of a field onto a prerequisite ``field``.

    ``kind`` is ``"hard"`` (the prerequisite's content is required to generate the field) or
    ``"soft"`` (helpful optional context). Maps onto the config's ``FieldDep`` when applied.
    """

    field: str
    kind: str


@dataclass(frozen=True)
class AutoSmartField:
    """The LLM's suggestion for one field: its generation ``type`` + ``prompt`` template.

    ``depends_on`` carries the model's proposed dependency edges onto other fields (empty when
    the model returned none).
    """

    type: str
    prompt: str
    depends_on: tuple[AutoSmartDep, ...] = ()
