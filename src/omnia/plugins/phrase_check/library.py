"""Putting a saved correction into the collection: the note type, the deck, and the note.

Everything here touches ``col``, which means it must run on the Qt main thread. The caller
marshals (``LookupService.call_on_main``); this module does the work and says what it did.

The name-picking is deliberately separate and pure (:func:`available_note_type_name`), because
it is the part with a rule worth arguing about — what to do when the name Omnia wants is already
somebody else's note type — and it should not need a collection to check.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Optional

from omnia.core import anki_compat
from omnia.core.logging import get_logger
from omnia.plugins.phrase_check import card

logger = get_logger("phrase_check")

#: Where saved corrections go unless the user says otherwise. A subdeck rather than the top
#: level: they are Omnia's, they accumulate, and they should be one click away from being
#: suspended or deleted as a group.
DEFAULT_DECK = "Omnia::Phrase Check"

#: How many "(copy N)" names to try before giving up. A collection with sixteen note types named
#: after this one has a problem no retry will fix.
_MAX_COPIES = 16


class SaveError(Exception):
    """A correction could not be saved. The message is meant to be shown as-is."""


@dataclass(frozen=True)
class Saved:
    """What a save actually did, for the sentence the clipper shows afterwards."""

    note_id: int
    deck: str
    note_type: str
    #: True when the requested note type name was taken by something that is not ours, so a
    #: "(copy)" was used instead. The clipper says so: silently writing into a name the user did
    #: not choose is how a note ends up somewhere they will never look.
    renamed: bool = False
    #: True when this phrase was already in the collection, so nothing new was added. Said out
    #: loud rather than passed off as a fresh save: "Saved" over a press that added nothing is
    #: the kind of small lie that has the user hunting for a card that is not where they think.
    already_there: bool = False

    def summary(self) -> str:
        """One sentence naming where it went."""
        if self.already_there:
            return f"Already saved in {self.deck}."
        where = f"Saved to {self.deck}"
        if self.renamed:
            return (
                f"{where}. The note type name you chose was taken, so “{self.note_type}” "
                f"was used — you can change it in Phrase Check's settings."
            )
        return f"{where}."


def is_ours(model: Any) -> bool:
    """Whether ``model`` is a note type this plugin can write into.

    By its FIELDS, not by its name: the name is the user's to change, and a note type they
    renamed is still ours. A stricter check (the template, the CSS) would refuse a note type
    somebody has restyled, which is a thing people are entitled to do to their own cards.
    """
    try:
        names = {field["name"] for field in model["flds"]}
    except (KeyError, TypeError):
        return False
    return set(card.FIELDS) <= names


def available_note_type_name(
    wanted: str, exists: Callable[[str], bool], ours: Callable[[str], bool]
) -> tuple[str, bool]:
    """The name to write under, and whether it had to differ from ``wanted``.

    Pure: ``exists`` and ``ours`` are the only questions it asks of a collection, so the rule can
    be checked without one.

    The rule, in order:

    1. the wanted name is free — use it;
    2. it is taken by one of ours — use it, because a note type we made and the user renamed is
       still the right home;
    3. it is taken by something else — do NOT write into it. Somebody else's note type has
       somebody else's fields, and a note added to it lands with our text in their "Front".
       Try ``name (copy)``, then ``(copy 2)``, and so on.

    Raises:
        SaveError: Every candidate is taken by something that is not ours.
    """
    if not exists(wanted) or ours(wanted):
        return wanted, False
    for index in range(1, _MAX_COPIES + 1):
        candidate = f"{wanted} (copy)" if index == 1 else f"{wanted} (copy {index})"
        if not exists(candidate) or ours(candidate):
            return candidate, True
    raise SaveError(
        f"“{wanted}” and every copy of it is taken by another note type. "
        "Pick a different name in Phrase Check's settings."
    )


def ensure_note_type(col: Any, wanted: str) -> tuple[Any, bool]:
    """Find or create the note type to save into; return it and whether the name had to change.

    Creating is idempotent by name, so a user who deletes it gets it back on the next save
    rather than an error about something they are allowed to delete.
    """
    name, renamed = available_note_type_name(
        wanted,
        exists=lambda candidate: col.models.by_name(candidate) is not None,
        ours=lambda candidate: is_ours(col.models.by_name(candidate)),
    )
    model = col.models.by_name(name)
    if model is not None:
        return model, renamed
    return _create_note_type(col, name), renamed


def _create_note_type(col: Any, name: str) -> Any:
    """Build the note type from the templates in :mod:`.card`."""
    model = col.models.new(name)
    for field_name in card.FIELDS:
        col.models.add_field(model, col.models.new_field(field_name))
    template = col.models.new_template(card.CARD_NAME)
    template["qfmt"] = card.front_template()
    template["afmt"] = card.back_template()
    col.models.add_template(model, template)
    model["css"] = card.card_css()
    col.models.add(model)
    logger.info("phrase_check: created the note type %r", name)
    return col.models.by_name(name)


def ensure_deck(col: Any, name: str) -> int:
    """The id of ``name``, creating the deck (and any parents) if it is not there."""
    wanted = (name or "").strip() or DEFAULT_DECK
    return int(col.decks.id(wanted))


def save_correction(
    col: Any,
    correction: Any,
    *,
    deck: str = DEFAULT_DECK,
    note_type: str = card.NOTE_TYPE_NAME,
) -> Saved:
    """Write one correction into the collection as a note.

    Must run on the Qt main thread — every line of it touches ``col``.

    Raises:
        SaveError: With a sentence the clipper shows as-is.
    """
    if not getattr(correction, "original", "").strip():
        raise SaveError("There is nothing to save.")
    try:
        model, renamed = ensure_note_type(
            col, (note_type or "").strip() or card.NOTE_TYPE_NAME
        )
        deck_id = ensure_deck(col, deck)
        fields = card.note_fields(correction)
        existing = find_existing(col, model, fields)
        if existing is not None:
            # Already here. Answered as a save rather than refused: from the reader's side
            # "keep this" has been honoured either way, and the alternative is an error about
            # something that is not wrong.
            return Saved(
                note_id=int(existing),
                deck=_deck_name(col, deck_id, deck),
                note_type=str(model["name"]),
                renamed=renamed,
                already_there=True,
            )
        note = col.new_note(model)
        for field, value in fields.items():
            note[field] = value
        col.add_note(note, deck_id)
    except SaveError:
        raise
    except Exception as exc:
        logger.exception("phrase_check: saving a correction failed")
        raise SaveError(f"Anki refused the note: {exc}") from exc
    deck_name = _deck_name(col, deck_id, deck)
    return Saved(
        note_id=int(getattr(note, "id", 0)),
        deck=deck_name,
        note_type=str(model["name"]),
        renamed=renamed,
    )


def find_existing(col: Any, model: Any, fields: dict[str, str]) -> Optional[int]:
    """The id of a note of ``model`` already holding this phrase in this register, or ``None``.

    Why a save is idempotent at all: one press can reach here twice. The HTTP hop gives up after
    five seconds and answers "nothing was saved" while the queued write still runs, so the retry
    that message invites is a second identical note — and the ordinary case of pressing Save
    twice does the same thing more simply.

    The key is phrase AND register, not phrase alone. The same sentence judged as spoken and as
    written is two different answers and two cards worth keeping; the panel treats switching
    register as a new question for exactly that reason, and deduplicating on the phrase alone
    would silently refuse the second one and hand back the first, which is the wrong card.

    The search is a FILTER and the comparison is the answer: Anki's search grammar treats ``*``
    and ``_`` as wildcards even inside quotes, and a phrase is arbitrary user text, so the query
    is escaped as well as it can be and every hit is then confirmed field-by-field. An escape
    this misses costs a duplicate — which is what happens today — never a wrong match.
    """
    phrase = fields.get(card.FIELD_PHRASE, "")
    register = fields.get(card.FIELD_REGISTER, "")
    if not phrase:
        return None
    query = (
        f'note:"{anki_compat.escape_search_term(str(model["name"]))}" '
        f'"{card.FIELD_PHRASE}:{_search_value(phrase)}"'
    )
    try:
        candidates = list(col.find_notes(query))
    except Exception:
        # A search this cannot express is not a reason to refuse the save. Falling through adds
        # the note, which is the behaviour without this check at all.
        logger.debug("phrase_check: duplicate search failed", exc_info=True)
        return None
    for nid in candidates:
        try:
            note = col.get_note(nid)
            if (
                note[card.FIELD_PHRASE] == phrase
                and note[card.FIELD_REGISTER] == register
            ):
                return int(nid)
        except Exception:  # a note deleted between the search and the read
            continue
    return None


def _search_value(text: str) -> str:
    """``text`` as a value inside a quoted Anki field search.

    Beyond :func:`~omnia.core.anki_compat.escape_search_term`'s backslash and quote, ``*`` and
    ``_`` are wildcards inside quotes too, so a phrase containing either would otherwise match
    more than itself. Over-matching is harmless here — every hit is confirmed — but it makes the
    search do work for nothing on a collection of any size.
    """
    escaped = anki_compat.escape_search_term(text)
    return escaped.replace("*", "\\*").replace("_", "\\_")


def _deck_name(col: Any, deck_id: int, fallback: str) -> str:
    """What the deck is actually called, for the sentence shown afterwards.

    Read back rather than echoed: Anki normalises a name (whitespace, separators, duplicates),
    so the deck someone ends up with is not always the string they typed.
    """
    try:
        deck = col.decks.get(deck_id)
        return str(deck["name"]) if deck else fallback
    except Exception:
        return fallback
