"""Language utilities shared by features: deterministic, dictionary-free text transforms.

The seam exists so two features never grow two subtly different de-inflectors. Everything in
here is pure stdlib and must stay free of ``aqt``/``anki`` imports (and, per the coupling
rule, of ``omnia.plugins``) so it unit-tests headless.
"""

from __future__ import annotations

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
    "word_boundary_pattern",
    "word_variants",
    "words_boundary_pattern",
]
