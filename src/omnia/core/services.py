"""A process-wide registry of capabilities one plugin publishes for another to consume.

Why this seam exists
--------------------
A feature the user has switched OFF must be **un-callable**, and an ``import`` cannot express
that: it binds the module for the life of the process, so a consumer holding another plugin's
object keeps a working handle long after ``on_disable`` ran. That would make Omnia's product
rule — "turning Smart Notes off leaves search working but makes generation unavailable" — a
promise the code has no way to keep. :class:`~omnia.core.plugin.PluginContext` deliberately
carries no handle to sibling plugins for the same reason, which leaves "is the provider alive
right now?" with no answer at all.

So the provider *publishes* an object while it is enabled and *withdraws* it when it is
disabled, and the consumer asks by NAME. A ``None`` from :func:`lookup` is the whole answer:
nobody is offering that capability at this moment. Names are plain strings (by convention
``"<plugin>.<capability>"``), so neither side imports the other — which is also what keeps the
``core`` → ``plugins`` coupling rule intact: this module knows nothing about who registers what,
or what any of it does.
"""

from __future__ import annotations

from typing import Optional

# name -> the object its provider published while enabled.
#
# NO LOCK, deliberately. ``provide``/``revoke`` run on the Qt main thread (plugin enable and
# disable) while ``lookup`` is called from non-Qt worker threads — an HTTP handler, say — but
# each of the three functions performs EXACTLY ONE dict operation, and a single dict
# get/setitem/pop is atomic in CPython (the GIL, and per-object locking in a free-threaded
# build). A reader therefore sees either the previous object or the new one, never a
# half-written map. A lock would only buy multi-key transactions, and nothing here reads two
# names as one.
_SERVICES: dict[str, object] = {}


def provide(name: str, service: object) -> None:
    """Publish ``service`` under ``name``, replacing any previous entry.

    Replacing rather than refusing a duplicate: a plugin re-enabled within one session must
    end up with ITS live object registered, not the dead one from the previous enable.
    """
    _SERVICES[name] = service


def revoke(name: str) -> None:
    """Withdraw ``name``'s service. Idempotent — revoking what was never provided is fine."""
    _SERVICES.pop(name, None)


def lookup(name: str) -> Optional[object]:
    """Return the object currently published under ``name``, or ``None`` when nobody is."""
    return _SERVICES.get(name)
