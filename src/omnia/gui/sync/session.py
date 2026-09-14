"""The one sharing session per profile, and its lifecycle.

Separate from the dialog because the socket outlives the window: a user turns sharing on, closes
the panel and walks over to the other machine, and that is the whole workflow. It also outlives
the *dialog class* — the profile hooks restore the session at startup and close it at shutdown,
and they must be able to do that without constructing any Qt.

So nothing here imports ``aqt``. The collection read the session serves is handed in as a
callable, exactly as :class:`~omnia.core.sync.service.Session` requires.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Optional

from omnia.core.logging import get_logger
from omnia.core.sync import MachineIdentity, MachineSettings, Session

logger = get_logger("sync")

_SESSION: Optional[Session] = None


def running() -> bool:
    """Whether this profile is answering right now."""
    return _SESSION is not None and _SESSION.running


def start(identity: MachineIdentity, inventory: Callable[[], Any]) -> bool:
    """(Re)build the session for ``identity`` and open its socket. Returns whether it serves.

    Rebuilt rather than mutated: the key and the port are what the socket was opened WITH, so a
    regenerated key or a changed port has to become a new socket or the old one keeps honouring
    the old ID.
    """
    global _SESSION
    stop()
    _SESSION = Session(
        inventory, key=identity.key, port=identity.port, packager=_packager()
    )
    if not _SESSION.start():
        _SESSION = None
        return False
    return True


def _packager() -> Any:
    """How this machine packs a selection a peer asked for.

    Imported here rather than at module load: it reaches Anki, and this module is also the one
    the profile hook uses before anything else is ready.
    """
    from omnia.gui.sync.export import build_package

    def pack(request: Any) -> Any:
        return build_package(request, None)

    return pack


def stop() -> None:
    """Close the socket. Safe when it was never open."""
    global _SESSION
    if _SESSION is not None:
        _SESSION.stop()
        _SESSION = None


def start_if_enabled(repo: Any, inventory: Callable[[], Any]) -> bool:
    """Restore sharing at profile open when the user left it on.

    The switch is a preference, not a session: someone who turned sharing on so the other machine
    could pull from this one did not mean "until I next quit Anki". ADR-020's first condition is
    that nothing shares until a person says so — this reads what that person said.

    A machine whose port is now taken simply does not come back up; the panel says so when it is
    next opened, and nothing here interrupts a profile load to report it.
    """
    settings = MachineSettings(repo)
    # The switch first, and through an accessor that mints NOTHING: `identity()` creates the
    # access code if there is none, and this runs on every profile open. Asking it the other way
    # round wrote a credential to every profile that had never opened the sync panel.
    if not settings.sharing():
        return False
    identity = settings.identity()
    if start(identity, inventory):
        return True
    logger.error(
        "sync: sharing was left on but port %s could not be opened", identity.port
    )
    return False
