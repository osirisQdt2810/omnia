"""Adding a pulled package to THIS collection, and the settings that came with it.

The last step of a pull, and the only one that changes anything. Everything before it reads: the
source exports, the bytes cross, and nothing on either machine has moved. So this is where the
care goes.

* **A backup first, always.** Anki's importer is transactional, but the thing a user fears is not
  a half-applied import — it is an import that worked exactly as asked and brought something they
  did not want. A backup is what makes that undoable, and it costs seconds against a pull that
  already took minutes.
* **Notes are updated, never duplicated.** Two machines syncing the same decks through AnkiWeb
  share note ids, so an arriving note usually already exists here. ``update_notes`` merges rather
  than making a second copy of every card the user has.
* **Note types are merged, not replaced.** A note type with the same name but different fields on
  this machine is a real situation — the user added a field here — and replacing it silently
  would empty that field on every note.
* **Settings are applied by section, and never credentials.** The source does not offer those and
  this refuses them again, because a list that arrives over a network is not one to trust because
  of where it came from.

Runs on the Qt main thread: it writes to the collection, and the caller shows a progress window.
"""

from __future__ import annotations

import os
from typing import Any

from omnia.core.logging import get_logger

logger = get_logger("sync")

#: Never applied, whatever a source says it is offering. ``llm``/``tts`` hold credentials;
#: ``sync`` is this machine's own identity and would hand two machines one ID; ``plugins`` is the
#: enable-map, which rides the collection already.
_NEVER_APPLIED = frozenset({"llm", "tts", "sync", "plugins", "log_level"})


class ApplyResult:
    """What an import changed, for the sentence shown afterwards."""

    def __init__(self) -> None:
        self.notes_added = 0
        self.notes_updated = 0
        #: How many notes the package held, whether or not any of them were new here.
        self.notes_found = 0
        self.sections: tuple[str, ...] = ()
        #: Note types created here from a definition that travelled on its own.
        self.note_types_added: tuple[str, ...] = ()

    @property
    def summary(self) -> str:
        """One line naming what arrived.

        Every kind of thing that can arrive gets a clause. A pull that created a note type and
        nothing else used to report "nothing new — this machine already had it all", telling the
        user the thing that just worked did not happen — and that sentence is what the picker's
        strip and the Sync button tooltip both show.
        """
        parts = []
        if self.notes_added:
            parts.append(_count(self.notes_added, "new note"))
        if self.notes_updated:
            parts.append(_count(self.notes_updated, "note") + " updated")
        if self.note_types_added:
            parts.append(_count(len(self.note_types_added), "note type"))
        if self.sections:
            parts.append(_count(len(self.sections), "setting"))
        if parts:
            return ", ".join(parts)
        if self.notes_found:
            # The difference that matters: the package arrived and held notes, and this machine
            # already had every one of them. "Nothing happened" would read as a failure.
            return f"nothing new — this machine already had all {self.notes_found:,} of them"
        return "nothing arrived"


def _count(number: int, noun: str) -> str:
    """``3, "new note"`` → ``"3 new notes"``."""
    return f"{number:,} {noun}" + ("" if number == 1 else "s")


def backup_first(reason: str = "before an Omnia sync") -> bool:
    """Take an Anki backup. Returns whether one was made.

    Before anything is written, and its failure is reported rather than fatal: a user who chose to
    copy a deck should not be stopped by a full disk on the backup folder — but they must be told,
    because the thing that makes an unwanted import survivable is this file.

    Waits for completion on the Qt thread, which is what Anki itself does before a risky
    operation. On a large collection that is a visible freeze with no progress window — seconds,
    against a copy that has already taken minutes — and it is the right trade: a backup taken
    after the import has started is not a backup.
    """
    from omnia.core import anki_compat

    try:
        col = anki_compat.main_window().col
        col.create_backup(
            backup_folder=_backup_folder(),
            force=True,
            wait_for_completion=True,
        )
    except Exception:
        logger.exception("sync: could not take a backup %s", reason)
        return False
    logger.info("sync: took a backup %s", reason)
    return True


def _backup_folder() -> str:
    """Where Anki keeps this profile's backups — its own folder, not one of ours.

    So the file shows up in Anki's own restore list. A backup Omnia hid somewhere else is one the
    user cannot find on the day they need it.
    """
    from aqt import mw

    return str(mw.pm.backupFolder())


def apply_package(path: str, policy: str = "") -> ApplyResult:
    """Import a pulled package into this collection.

    Args:
        path: The ``.apkg`` that arrived.
        policy: What to do with a note that exists on both machines — ``keep`` (the default) or
            ``override``.

    Returns:
        What changed.

    Raises:
        Exception: Whatever Anki's importer raises. The caller turns it into a sentence; nothing
            is swallowed here, because a failed import that reported success is the worst outcome
            this feature has.
    """
    from anki.collection import ImportAnkiPackageOptions, ImportAnkiPackageRequest

    from omnia.core import anki_compat

    col = anki_compat.main_window().col
    changes = col.import_anki_package(
        ImportAnkiPackageRequest(
            package_path=path,
            options=ImportAnkiPackageOptions(
                merge_notetypes=True,
                update_notes=_condition(policy),
                # Note types always follow IF_NEWER, whatever the note policy says. The choice
                # the user made is about their NOTES; silently replacing a note type they edited
                # here would empty a field on every note that used it, which is not something to
                # infer from an answer to a different question.
                update_notetypes=_if_newer(),
                with_scheduling=True,
                with_deck_configs=True,
            ),
        )
    )
    result = ApplyResult()
    log = getattr(changes, "log", None)
    if log is not None:
        # ``new`` and ``updated`` are repeated Notes on ImportResponse.Log; ``found_notes`` is
        # how many the package held, which is what makes "nothing new" readable as "this machine
        # already had it all" rather than as "the package was empty".
        result.notes_added = len(getattr(log, "new", ()) or ())
        result.notes_updated = len(getattr(log, "updated", ()) or ())
        result.notes_found = int(getattr(log, "found_notes", 0) or 0)
    logger.info(
        "sync: imported %s — %d new, %d updated",
        os.path.basename(path),
        result.notes_added,
        result.notes_updated,
    )
    return result


def _condition(policy: str) -> int:
    """Anki's update condition for the policy the user chose.

    ``keep`` is NEVER rather than IF_NEWER: "leave what is here alone" has to mean exactly that,
    and a note that is newer over there is still a note this machine already has.
    """
    from anki.import_export_pb2 import ImportAnkiPackageUpdateCondition as Condition

    from omnia.core.sync.clash import OVERRIDE, normalise

    if normalise(policy) == OVERRIDE:
        return int(Condition.IMPORT_ANKI_PACKAGE_UPDATE_CONDITION_ALWAYS)
    return int(Condition.IMPORT_ANKI_PACKAGE_UPDATE_CONDITION_NEVER)


def _if_newer() -> int:
    """Anki's "update only when the arriving one is newer" — what NOTE TYPES are imported under.

    Not a choice the user is asked to make. One with this name may differ here because they added
    a field on this machine, and replacing it wholesale empties that field on every note using
    it; making a second copy of it instead would split their notes across two note types that
    look identical. IF_NEWER is the only one of the three that does neither.
    """
    from anki.import_export_pb2 import ImportAnkiPackageUpdateCondition

    return int(
        ImportAnkiPackageUpdateCondition.IMPORT_ANKI_PACKAGE_UPDATE_CONDITION_IF_NEWER
    )


def apply_note_types(definitions: Any) -> tuple[str, ...]:
    """Create note types that travelled as definitions rather than inside a package.

    Only the ones that are NOT already here. An existing note type is left exactly as it is: the
    user asked to bring a kind of card this machine did not have, not to have their own version
    of one replaced — and replacing it is what empties a field on every note that used it.

    Args:
        definitions: Note types as Anki stores them, from the other machine.

    Returns:
        The names actually created, in order.
    """
    from omnia.core import anki_compat

    col = anki_compat.main_window().col
    created: list[str] = []
    for definition in definitions or ():
        name = str((definition or {}).get("name", ""))
        if not name:
            continue
        try:
            if col.models.by_name(name):
                logger.info("sync: note type %r is already here, left alone", name)
                continue
            model = dict(definition)
            # The id and the modification stamp belong to the OTHER collection. Cleared so Anki
            # allocates its own; keeping them would collide with whatever holds that id here.
            model["id"] = 0
            model.pop("usn", None)
            col.models.add_dict(model)
        except Exception:
            logger.exception("sync: could not create the note type %r", name)
            continue
        created.append(name)
    return tuple(created)


def apply_config(repo: Any, sections: Any) -> tuple[str, ...]:
    """Copy the chosen configuration sections onto this machine.

    Args:
        repo: The config repository.
        sections: ``{name: values}`` as the source sent them.

    Returns:
        The sections actually applied, in order. Anything on the refuse-list is dropped here as
        well as on the source: a list that arrived over a network is not to be trusted because of
        where it came from.
    """
    if not isinstance(sections, dict):
        return ()
    applied: list[str] = []
    for name in sorted(sections):
        if name in _NEVER_APPLIED:
            logger.warning("sync: refused to apply the %r section", name)
            continue
        values = sections[name]
        if not isinstance(values, dict):
            continue
        try:
            repo.update_section(name, values)
        except Exception:
            logger.exception("sync: could not apply the %r settings", name)
            continue
        applied.append(name)
    return tuple(applied)
