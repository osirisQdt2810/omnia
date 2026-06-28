"""Tiny loader for GUI asset files that ship next to a module.

The settings page and every per-plugin GUI keep their markup (HTML/CSS/JS) in a sibling
``web/`` folder rather than as Python string literals. :func:`read_asset` resolves a file
relative to the *calling* module's directory via :class:`pathlib.Path`, so it works both
in the symlinked ``src/omnia`` dev install AND inside the packaged ``.ankiaddon`` zip
(module-relative paths, never web-export). :func:`read_assets` reads several pieces of one
split blob (a large CSS/JS file cut into cohesive parts) and concatenates them in an
explicit, deterministic order.

This module imports nothing from ``aqt``/``anki`` so it tests headless.
"""

from __future__ import annotations

from pathlib import Path


def read_asset(module_file: str, *parts: str) -> str:
    """Read a UTF-8 asset that ships next to ``module_file``.

    Args:
        module_file: The caller's ``__file__``; the asset is resolved relative to its
            containing directory.
        *parts: Path components of the asset (e.g. ``"web", "page.css"``).

    Returns:
        The asset's text content.
    """
    return (
        Path(module_file).resolve().parent.joinpath(*parts).read_text(encoding="utf-8")
    )


def read_assets(module_file: str, *parts: str, names: list[str]) -> str:
    """Read several assets from one directory and join them with ``"\\n"`` in order.

    A large CSS/JS blob is split into cohesive pieces on disk; the page builder reads them
    back in the EXPLICIT order given by ``names`` and concatenates with a single newline.
    Joining the pieces this way reproduces the original single file byte-for-byte (each piece
    is the text between the original newlines, with the join re-inserting one ``"\\n"``).

    Args:
        module_file: The caller's ``__file__``; the directory is resolved relative to it.
        *parts: Path components of the directory holding the pieces (e.g. ``"web"``).
        names: The piece filenames in load order (deterministic, reviewable — not globbed).

    Returns:
        The pieces' text content joined by ``"\\n"``.
    """
    directory = Path(module_file).resolve().parent.joinpath(*parts)
    return "\n".join((directory / name).read_text(encoding="utf-8") for name in names)
