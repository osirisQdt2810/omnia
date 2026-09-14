"""Packing what the target asked for, on the SOURCE machine.

The other half of :mod:`omnia.gui.sync.collect`: that one answers "what do you have", this one
answers "then send me that". Both are the only modules on the source side that touch Anki.

Three things leave this machine, and only the first is a file:

* **cards**, as an ``.apkg`` with their media. The selection is a filter on CARDS, not on decks —
  the user picks decks and may drop note types, and a deck holding two of them with one dropped
  must arrive with only the other's notes — so the search is built first and the export is handed
  the resulting card ids.
* **note type definitions**, for the ones chosen with no notes behind them. These cannot ride in
  the package: Anki gathers note types from the notes it is exporting, so one with no cards
  contributes nothing at all. They travel as data in the answer.
* **settings**, for the same reason — they are not Anki's to store.

The search string is built with Anki's own :class:`~anki.collection.SearchNode`, never with
f-strings: a deck called ``Chemistry "hard"`` or a note type with a colon in it would otherwise
produce a search that silently matches something else.

It runs on a worker thread holding the collection. Not a shortcut — Anki's own exporter does
exactly this (``aqt.import_export.exporting`` hands ``col`` to a ``QueryOp``) — and the
alternative is freezing the source machine for however long a few hundred megabytes of media
takes.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from typing import Any

from omnia.core.logging import get_logger
from omnia.core.sync.package import PackageError, PackageOffer, PackageRequest

logger = get_logger("sync")

#: Where packages are written. Under the system temp dir rather than in the collection folder: a
#: half-written ``.apkg`` beside a collection is something Anki's media check will ask about, and
#: the file is deleted as soon as the other machine has it.
_PREFIX = "omnia-sync-"

#: Config sections that never leave, whatever a peer asks for. Credentials are the owner's; the
#: ``sync`` section is this machine's own identity and would hand two machines one ID; ``plugins``
#: is the enable-map, which already rides the collection.
_NEVER_SENT = frozenset({"llm", "tts", "sync", "plugins", "log_level"})


def packager(repo: Any) -> Any:
    """The callable a :class:`~omnia.core.sync.service.Session` uses to pack a selection."""

    def pack(request: PackageRequest) -> tuple[str, PackageOffer]:
        return build_package(request, repo)

    return pack


def build_package(request: PackageRequest, repo: Any) -> tuple[str, PackageOffer]:
    """Gather what ``request`` names, and describe what came out.

    Args:
        request: What the other machine asked for.
        repo: The config repository, for the settings that travel beside the package.

    Returns:
        ``(path, offer)``. ``path`` is ``""`` when nothing needed a file — a request for note
        type definitions or settings alone produces no ``.apkg`` at all, and inventing an empty
        one would mean the target downloading a file to learn there is nothing in it.

    Raises:
        PackageError: When the selection matches nothing on this machine. Refused rather than
            exported: a zero-card package arriving after a long wait looks exactly like a bug,
            and the real problem is a choice that no longer matches this collection.
    """
    config = _config_sections(request, repo)
    definitions = _note_type_definitions(request)

    if not request.wants_cards:
        # Note types or settings alone. There is no card search to run — and running one would be
        # catastrophic rather than merely wrong, because a card search with no deck in it is not
        # a narrow search, it is the whole collection.
        if not config and not definitions:
            raise PackageError(
                "nothing on this machine matches what was chosen — those note types or "
                "settings may have been renamed or removed since the list was made"
            )
        logger.info(
            "sync: sending %d note type definition(s) and %d setting(s), no cards",
            len(definitions),
            len(config),
        )
        return "", PackageOffer(
            id=uuid.uuid4().hex, config=config, note_types=definitions
        )

    card_ids = _card_ids(_collection(), request)
    if not card_ids:
        raise PackageError(
            "nothing on this machine matches what was chosen — those decks may have been "
            "renamed or emptied since the list was made"
        )
    path, notes = _export(card_ids, request)
    size = os.path.getsize(path) if os.path.exists(path) else 0
    logger.info(
        "sync: packed %d card(s) from %d deck(s) into %d bytes",
        len(card_ids),
        len(request.decks),
        size,
    )
    return path, PackageOffer(
        id=uuid.uuid4().hex,
        bytes=size,
        cards=len(card_ids),
        notes=notes,
        config=config,
        note_types=definitions,
    )


def _export(card_ids: list[int], request: PackageRequest) -> tuple[str, int]:
    """Write the ``.apkg``. Returns ``(path, notes exported)``."""
    from anki.collection import CardIdsLimit, ExportAnkiPackageOptions

    handle, path = tempfile.mkstemp(prefix=_PREFIX, suffix=".apkg")
    os.close(handle)
    try:
        notes = _collection().export_anki_package(
            out_path=path,
            options=ExportAnkiPackageOptions(
                with_scheduling=request.with_scheduling,
                with_deck_configs=True,
                with_media=request.with_media,
                legacy=False,
            ),
            limit=CardIdsLimit(card_ids=card_ids),
        )
    except Exception:
        _discard(path)
        raise
    return path, int(notes or 0)


def _card_ids(col: Any, request: PackageRequest) -> list[int]:
    """The cards the request names: in one of those decks, AND of one of those note types.

    Returns ``[]`` for a request that names no decks, and never builds a search for one. Anki
    reads an empty search as the whole collection, so an unguarded ``find_cards("")`` here would
    export every card and every media file this machine owns — the exact widening this feature
    refuses everywhere else.
    """
    from anki.collection import SearchNode

    if not request.decks:
        return []
    decks = col.group_searches(
        *(SearchNode(deck=name) for name in request.decks), joiner="OR"
    )
    if not request.note_types:
        # Also unreachable through PackageRequest, which refuses decks with no note types. Guarded
        # anyway: the cost of being wrong here is the whole collection.
        return []
    note_types = col.group_searches(
        *(SearchNode(note=name) for name in request.note_types), joiner="OR"
    )
    return [
        int(cid) for cid in col.find_cards(col.join_searches(decks, note_types, "AND"))
    ]


def _note_type_definitions(request: PackageRequest) -> tuple[dict[str, Any], ...]:
    """The note types that must travel as DATA rather than inside the package.

    Only the ones no chosen deck can drag along — with decks named, the package carries whatever
    its notes use. A definition sent twice would be harmless and a definition sent for every note
    type in a large collection would not, so this stays narrow.
    """
    if request.decks or not request.note_types:
        return ()
    col = _collection()
    out: list[dict[str, Any]] = []
    for name in request.note_types:
        try:
            model = col.models.by_name(name)
        except Exception:
            model = None
        if model:
            out.append(dict(model))
    return tuple(out)


def _config_sections(request: PackageRequest, repo: Any) -> dict[str, Any]:
    """The settings that were asked for, minus the ones that never leave."""
    if not request.config or repo is None:
        return {}
    out: dict[str, Any] = {}
    for name in request.config:
        if name in _NEVER_SENT:
            logger.warning("sync: refused to send the %r section", name)
            continue
        try:
            values = repo.raw_section(name)
        except Exception:
            logger.exception("sync: could not read the %r settings", name)
            continue
        if values:
            out[name] = values
    return out


def _collection() -> Any:
    from omnia.core import anki_compat

    return anki_compat.main_window().col


def _discard(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        logger.warning("sync: could not remove the half-written package at %s", path)
