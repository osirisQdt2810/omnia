"""Tiny loader for GUI asset files that ship next to a module.

The settings page and every per-plugin GUI keep their markup (HTML/CSS/JS) in sibling
asset files rather than as Python string literals. :func:`read_asset` resolves a file
relative to the *calling* module's directory via :class:`pathlib.Path`, so it works both
in the symlinked ``src/omnia`` dev install AND inside the packaged ``.ankiaddon`` zip
(module-relative paths, never web-export).

This module imports nothing from ``aqt``/``anki`` so it tests headless.
"""

from __future__ import annotations

from pathlib import Path


def read_asset(module_file: str, *parts: str) -> str:
    """Read a UTF-8 asset that ships next to ``module_file``.

    Args:
        module_file: The caller's ``__file__``; the asset is resolved relative to its
            containing directory.
        *parts: Path components of the asset (e.g. ``"page.css"`` or ``"web", "x.js"``).

    Returns:
        The asset's text content.
    """
    return (
        Path(module_file).resolve().parent.joinpath(*parts).read_text(encoding="utf-8")
    )
