"""Reading THIS collection into the inventory a peer is served.

The one module on the source side that touches Anki. It runs on the session's worker thread, so
every collection read is marshalled onto the Qt main thread and waited for with a deadline — the
same rule the lookup service follows, for the same reason: Anki's collection is main-thread-only,
and a read that cannot get there must become a "busy" answer rather than a hung request.

What it gathers is a menu, not the goods: deck names and card counts, note types and their field
names, and the NAMES of what is configured. No card contents, no media, no rules.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from omnia.core import anki_compat
from omnia.core.logging import get_logger
from omnia.core.sync.inventory import (
    ConfigSummary,
    DeckEntry,
    Inventory,
    NoteTypeEntry,
)
from omnia.core.sync.service import MAIN_THREAD_TIMEOUT_SECONDS

logger = get_logger("sync")

T = TypeVar("T")

#: Config sections that are never offered, never mind pulled. Credentials do not travel (the
#: owner's decision, recorded with the feature), and the plugin enable-map is a property of the
#: collection rather than of a machine.
_NEVER_OFFERED = frozenset({"llm", "tts", "sync", "plugins", "log_level"})


def inventory_reader(repo: Any) -> Callable[[], Inventory]:
    """The collection-reading callable a :class:`~omnia.core.sync.service.Session` serves.

    Written once because two callers need the same one: the dialog, when sharing is switched on,
    and the profile hook, which restores a session without ever building a dialog.
    """
    return lambda: read_inventory(repo)


def read_inventory(repo: Any) -> Inventory:
    """Build the inventory this machine offers.

    Args:
        repo: The config repository, for the configuration summary.

    Returns:
        The inventory.

    Raises:
        TimeoutError: When the Qt main thread could not be reached in time. The session turns
            that into a 503 and the other machine renders "busy" — which is the honest answer,
            and the reason this is not swallowed here.
    """
    decks, note_types = _from_collection()
    return Inventory(
        machine=_machine_name(),
        omnia_version=_version(),
        decks=decks,
        note_types=note_types,
        config=_config_summary(repo),
    )


def _from_collection() -> tuple[tuple[DeckEntry, ...], tuple[NoteTypeEntry, ...]]:
    """The decks and note types, read on the Qt main thread with a deadline."""

    def _read() -> tuple[tuple[DeckEntry, ...], tuple[NoteTypeEntry, ...]]:
        col = anki_compat.main_window().col
        cards = _card_counts(col)
        notes = _note_counts(col)
        names = {int(model["id"]): str(model["name"]) for model in col.models.all()}
        used = _note_types_per_deck(col, names)
        decks = tuple(
            DeckEntry(
                id=deck_id,
                name=name,
                cards=cards.get(deck_id, 0),
                note_types=tuple(sorted(used.get(deck_id, ()))),
            )
            for deck_id, name in anki_compat.deck_names(col)
        )
        note_types = tuple(
            NoteTypeEntry(
                name=str(model["name"]),
                fields=tuple(str(field["name"]) for field in model["flds"]),
                notes=notes.get(int(model["id"]), 0),
            )
            for model in col.models.all()
        )
        return decks, note_types

    return call_on_main(_read)


def call_on_main(work: Callable[[], T]) -> T:
    """Run ``work`` on the Qt main thread and return its value, or time out.

    Anki's collection is main-thread-only while this runs on the session's worker thread, so
    every read has to cross over — and a crossing without a deadline is a request that hangs
    the other machine's dialog for ever instead of telling it the truth.

    Raises:
        TimeoutError: The main thread did not run the work in time. The session turns that into
            a 503 and the peer renders "busy".
    """
    import threading

    box: dict[str, Any] = {}
    done = threading.Event()

    def runner() -> None:
        try:
            box["value"] = work()
        except Exception as exc:  # carried back to the requesting thread
            box["error"] = exc
        finally:
            done.set()

    anki_compat.run_on_main(runner)
    if not done.wait(MAIN_THREAD_TIMEOUT_SECONDS):
        raise TimeoutError("Anki's main thread did not answer in time")
    if "error" in box:
        raise box["error"]
    return box["value"]  # type: ignore[no-any-return]


def _card_counts(col: Any) -> dict[int, int]:
    """Cards per deck, in ONE query — ``{deck_id: cards}``, sub-decks not included.

    A search per deck is what this replaced, and on a real collection it cost four and a half
    seconds with Anki frozen for every one of them: 1,914 decks meant 1,914 searches, run on the
    main thread because that is where the collection lives. One aggregate is a few milliseconds,
    and the counts are the same numbers.

    A card in a filtered deck is counted against the deck it came FROM (``odid``), which is
    where the user sees it and where it returns to.

    Returns:
        The counts, or ``{}`` when the query is unavailable — every deck then reads 0, which is
        wrong but harmless: the count is decoration and must never be why the menu fails.
    """
    try:
        rows = col.db.all(
            "select case when odid != 0 then odid else did end as home, count() "
            "from cards group by home"
        )
        return {int(home): int(total) for home, total in rows}
    except Exception:
        logger.warning("sync: could not count cards per deck", exc_info=True)
        return {}


def _note_types_per_deck(col: Any, names: dict[int, str]) -> dict[int, set[str]]:
    """Which note types each deck's own cards use — ``{deck_id: {name, ...}}``, in one query.

    The picker lights up a note type the instant a deck that needs it is chosen, so this has to
    arrive WITH the inventory; asking per deck would be a network round trip per click, over a
    link that may be a laptop on the other side of a VPN.

    One ``distinct`` pass over the card table rather than a query per deck, for the same reason
    the counts are one aggregate: this runs on Anki's main thread and everything on it is frozen
    while it does.

    Returns:
        The map, or ``{}`` when the query is unavailable — the picker then lights nothing up,
        which is a duller panel and not a broken one.
    """
    try:
        rows = col.db.all(
            "select distinct case when c.odid != 0 then c.odid else c.did end as home, n.mid "
            "from cards c join notes n on n.id = c.nid"
        )
    except Exception:
        logger.warning("sync: could not map decks to note types", exc_info=True)
        return {}
    out: dict[int, set[str]] = {}
    for home, mid in rows:
        name = names.get(int(mid))
        if name:
            out.setdefault(int(home), set()).add(name)
    return out


def _note_counts(col: Any) -> dict[int, int]:
    """Notes per note type, in one query — ``{note_type_id: notes}``."""
    try:
        return {
            int(mid): int(total)
            for mid, total in col.db.all("select mid, count() from notes group by mid")
        }
    except Exception:
        logger.warning("sync: could not count notes per note type", exc_info=True)
        return {}


def _machine_name() -> str:
    """What to call this computer in the other one's panel."""
    import socket

    try:
        return socket.gethostname() or "another computer"
    except Exception:
        return "another computer"


def _version() -> str:
    """The add-on's own version, read from the manifest the build stamps it into.

    Walked up to the PACKAGE directory rather than by a parent index: this file is
    ``omnia/gui/sync/collect.py`` and the manifest sits beside ``omnia/__init__.py``, and an
    index off by one is an empty version with no error anywhere — which is exactly how the first
    live run of this reported no version at all.
    """
    import json
    from pathlib import Path

    try:
        package = Path(__file__).resolve().parent.parent.parent  # .../omnia
        raw = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
        return str(raw.get("human_version") or "")
    except Exception:
        # A source checkout has no stamped version — build_addon.py adds it — and the peer only
        # SHOWS this; it never branches on it. The protocol number decides compatibility.
        return ""


def _config_summary(repo: Any) -> ConfigSummary:
    """Which features are configured, which note types the rules cover, which tools exist."""
    features: list[str] = []
    for section in sorted(_configured_sections(repo)):
        if section not in _NEVER_OFFERED:
            features.append(section)
    return ConfigSummary(
        features=tuple(features),
        rule_note_types=tuple(_rule_note_types(repo)),
        tools=(),
    )


def _configured_sections(repo: Any) -> list[str]:
    """Which top-level config sections hold anything, asked one plugin id at a time.

    Through the registry rather than by walking the merged dict: ``raw_section`` is the read
    side the repository offers, and a plugin whose section is absent simply answers ``{}``.
    """
    from omnia.core.registry import FEATURE_REGISTRY

    found: list[str] = []
    for plugin_id in FEATURE_REGISTRY:
        try:
            if repo.raw_section(plugin_id):
                found.append(plugin_id)
        except Exception:
            continue
    return found


def _rule_note_types(repo: Any) -> list[str]:
    """The note types Smart Notes has rules for, read without importing the plugin.

    By SECTION rather than through the plugin: ``core`` may not import ``plugins`` (the coupling
    rule), and the summary only needs the names.
    """
    try:
        section = repo.raw_section("smart_notes")
    except Exception:
        return []
    note_types = section.get("note_types")
    if not isinstance(note_types, list):
        return []
    return [
        str(entry.get("note_type", ""))
        for entry in note_types
        if isinstance(entry, dict) and entry.get("note_type")
    ]
