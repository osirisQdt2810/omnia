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
        self.sections: tuple[str, ...] = ()
        self.log_text = ""

    @property
    def summary(self) -> str:
        """One line naming what arrived."""
        parts = []
        if self.notes_added:
            parts.append(
                f"{self.notes_added:,} new note"
                + ("" if self.notes_added == 1 else "s")
            )
        if self.notes_updated:
            parts.append(
                f"{self.notes_updated:,} note"
                + ("" if self.notes_updated == 1 else "s")
                + " updated"
            )
        if self.sections:
            parts.append(
                f"{len(self.sections)} setting"
                + ("" if len(self.sections) == 1 else "s")
            )
        return (
            ", ".join(parts)
            if parts
            else "nothing new — this machine already had it all"
        )


def backup_first(reason: str = "before an Omnia sync") -> bool:
    """Take an Anki backup. Returns whether one was made.

    Before anything is written, and its failure is reported rather than fatal: a user who chose to
    copy a deck should not be stopped by a full disk on the backup folder — but they must be told,
    because the thing that makes an unwanted import survivable is this file.
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


def apply_package(path: str) -> ApplyResult:
    """Import a pulled package into this collection.

    Args:
        path: The ``.apkg`` that arrived.

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
                update_notes=_if_newer(),
                update_notetypes=_if_newer(),
                with_scheduling=True,
                with_deck_configs=True,
            ),
        )
    )
    result = ApplyResult()
    log = getattr(changes, "log", None)
    if log is not None:
        result.notes_added = len(getattr(log, "new", []) or [])
        result.notes_updated = len(getattr(log, "updated", []) or [])
    logger.info(
        "sync: imported %s — %d new, %d updated",
        os.path.basename(path),
        result.notes_added,
        result.notes_updated,
    )
    return result


def _if_newer() -> int:
    """Anki's "update only when the arriving one is newer".

    The condition both notes and note types are imported under, and the reason is the same for
    both: what arrives usually already exists here. Two machines syncing the same decks through
    AnkiWeb share note ids, so ALWAYS would overwrite edits made on this machine and NEVER would
    make a second copy of every card — doubling the collection in one click.

    Sharper still for note types: one with this name may differ here because the user added a
    field on this machine, and replacing it wholesale empties that field on every note using it.
    """
    from anki.import_export_pb2 import ImportAnkiPackageUpdateCondition

    return int(
        ImportAnkiPackageUpdateCondition.IMPORT_ANKI_PACKAGE_UPDATE_CONDITION_IF_NEWER
    )


def apply_config(repo: Any, sections: dict[str, Any]) -> tuple[str, ...]:
    """Copy the chosen configuration sections onto this machine.

    Args:
        repo: The config repository.
        sections: ``{name: values}`` as the source sent them.

    Returns:
        The sections actually applied, in order. Anything on the refuse-list is dropped here as
        well as on the source: a list that arrived over a network is not to be trusted because of
        where it came from.
    """
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
