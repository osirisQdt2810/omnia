"""Packing a chosen selection on the SOURCE machine.

The other half of :mod:`omnia.gui.sync.collect`: that one answers "what do you have", this one
answers "then send me that". Both are the only modules on the source side that touch Anki.

Two things decide the shape:

* **The selection is a filter on CARDS, not on decks.** The user picks decks and may drop note
  types, and a deck holding two note types with one dropped must arrive with only the other's
  notes. Anki expresses that as a card-id limit, so the search is built first and the export is
  handed the result.
* **It runs on a worker thread, holding the collection.** Not a shortcut — Anki's own exporter
  does exactly this (``aqt.import_export.exporting`` hands ``col`` to a ``QueryOp``) — and the
  alternative is freezing the source machine for however long it takes to copy a few hundred
  megabytes of media.

The search string is built with Anki's own :class:`~anki.collection.SearchNode`, never with
f-strings: a deck called ``Chemistry "hard"`` or a note type with a colon in it would otherwise
produce a search that silently matches something else.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from typing import Any

from omnia.core.logging import get_logger
from omnia.core.sync.package import PackageError, PackageOffer, PackageRequest

logger = get_logger("sync")

#: Where packages are written. Under the system temp dir rather than in the collection folder:
#: a half-written ``.apkg`` beside a collection is something Anki's media check will ask about,
#: and the file is deleted as soon as the other machine has it.
_PREFIX = "omnia-sync-"


def packager(repo: Any) -> Any:
    """The callable a :class:`~omnia.core.sync.service.Session` uses to pack a selection."""

    def pack(request: PackageRequest) -> tuple[str, PackageOffer]:
        return build_package(request, repo)

    return pack


def build_package(request: PackageRequest, _repo: Any) -> tuple[str, PackageOffer]:
    """Export what ``request`` names, and describe what came out.

    Args:
        request: What the other machine asked for.
        _repo: The config repository. Unused today — configuration travels as names in the same
            answer rather than inside the package — and named so the seam is visible when it is.

    Returns:
        ``(path, offer)``: where the package is, and what it holds.

    Raises:
        PackageError: When the selection matches nothing. Refused rather than exported: a
            zero-card ``.apkg`` arriving after a long wait looks exactly like a bug, and the real
            problem is a choice that no longer matches this collection.
    """
    from anki.collection import ExportAnkiPackageOptions

    from omnia.core import anki_compat

    col = anki_compat.main_window().col
    card_ids = _card_ids(col, request)
    if not card_ids:
        raise PackageError(
            "nothing on this machine matches what was chosen — those decks may have been "
            "renamed or emptied since the list was made"
        )

    handle, path = tempfile.mkstemp(prefix=_PREFIX, suffix=".apkg")
    os.close(handle)
    try:
        notes = col.export_anki_package(
            out_path=path,
            options=ExportAnkiPackageOptions(
                with_scheduling=request.with_scheduling,
                with_deck_configs=True,
                with_media=request.with_media,
                legacy=False,
            ),
            limit=_limit(card_ids),
        )
    except Exception:
        _discard(path)
        raise
    size = os.path.getsize(path) if os.path.exists(path) else 0
    logger.info(
        "sync: packed %d card(s) from %d deck(s) into %d bytes",
        len(card_ids),
        len(request.decks),
        size,
    )
    return path, PackageOffer(
        id=uuid.uuid4().hex, bytes=size, cards=len(card_ids), notes=int(notes or 0)
    )


def _card_ids(col: Any, request: PackageRequest) -> list[int]:
    """The cards the request names: in one of those decks, AND of one of those note types."""
    from anki.collection import SearchNode

    decks = col.group_searches(
        *(SearchNode(deck=name) for name in request.decks), joiner="OR"
    )
    if not request.note_types:
        return [int(cid) for cid in col.find_cards(decks)]
    note_types = col.group_searches(
        *(SearchNode(note=name) for name in request.note_types), joiner="OR"
    )
    search = col.join_searches(decks, note_types, "AND")
    return [int(cid) for cid in col.find_cards(search)]


def _limit(card_ids: list[int]) -> Any:
    """Anki's way of saying "these cards and no others"."""
    from anki.collection import CardIdsLimit

    return CardIdsLimit(card_ids=card_ids)


def _discard(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        logger.warning("sync: could not remove the half-written package at %s", path)
