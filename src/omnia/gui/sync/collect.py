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
        decks = tuple(
            DeckEntry(id=deck_id, name=name, cards=_card_count(col, name))
            for deck_id, name in anki_compat.deck_names(col)
        )
        note_types = tuple(
            NoteTypeEntry(
                name=name,
                fields=tuple(anki_compat.note_type_field_names(name, col)),
                notes=_note_count(col, name),
            )
            for name in anki_compat.note_type_names(col)
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


def _card_count(col: Any, deck: str) -> int:
    """Cards in ``deck`` ALONE. ``-deck:x::*`` excludes the sub-decks, whose own rows say so."""
    try:
        return len(col.find_cards(f'"deck:{deck}" -"deck:{deck}::*"'))
    except Exception:
        # A deck name with a quote in it, or a search Anki refuses: the count is decoration and
        # must never be the reason the whole menu fails to build.
        return 0


def _note_count(col: Any, note_type: str) -> int:
    try:
        return len(col.find_notes(f'"note:{note_type}"'))
    except Exception:
        return 0


def _machine_name() -> str:
    """What to call this computer in the other one's panel."""
    import socket

    try:
        return socket.gethostname() or "another computer"
    except Exception:
        return "another computer"


def _version() -> str:
    """The add-on's own version, read from the manifest the build stamped it into."""
    import json
    from pathlib import Path

    try:
        manifest = Path(__file__).resolve().parents[3] / "manifest.json"
        return str(
            json.loads(manifest.read_text(encoding="utf-8")).get("human_version", "")
        )
    except Exception:
        # A dev checkout has no stamped version, and the peer only shows it — never branches
        # on it. The protocol number is what decides compatibility.
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
