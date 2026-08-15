"""Language utilities shared by features: deterministic, dictionary-free text transforms.

Two kinds live here, and both are seams for the same reason — so two features never grow two
subtly different answers to the same question: de-inflection (``word_forms``) and the
conversion between a note field's stored markup and the words a person actually wrote
(``text``). Everything in here is pure stdlib and must stay free of ``aqt``/``anki`` imports
(and, per the coupling rule, of ``omnia.plugins``) so it unit-tests headless.
"""

from __future__ import annotations

from omnia.core.lang.text import as_field_html, strip_markup
from omnia.core.lang.word_forms import (
    DEFAULT_DEINFLECTOR,
    UNAMBIGUOUS_IRREGULAR,
    Deinflector,
    word_boundary_pattern,
    word_variants,
    words_boundary_pattern,
)

__all__ = [
    "DEFAULT_DEINFLECTOR",
    "UNAMBIGUOUS_IRREGULAR",
    "Deinflector",
    "as_field_html",
    "strip_markup",
    "word_boundary_pattern",
    "word_variants",
    "words_boundary_pattern",
]
