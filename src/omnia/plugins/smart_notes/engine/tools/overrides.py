"""Editing a builtin: the user's own copy of a shipped tool, loaded over it.

``cloze``, ``cloze_audio`` and ``ai`` ship as Python in this package. A user who wants one of
them to behave differently — a different hint shape, a different decline rule, a provider call
their deck needs — has no way to say so through params alone, and the tool seam's whole point is
that a tool is a small readable class. So the Tools tab can open a builtin's REAL source in the
same editor user tools use, and what comes back is saved as an override.

An override is a file in ``user_files/tools/builtin/<name>.py`` that registers the builtin's own
name. Three consequences, all deliberate:

* **Every field already configured with that tool gets the edit**, with nothing to re-pick. That
  is what "edit the builtin" has to mean; a copy under a second name would leave the user's
  existing chains running the shipped code and looking like the edit did nothing.
* **The shipped class is remembered, not lost.** The loader holds it and puts it back when the
  override is deleted or the feature is disabled, so "Restore built-in" is exact rather than a
  re-import, and a session that never restarts still ends up on the real tool.
* **A broken override never costs the builtin.** The file is compiled before it is written and
  compiled again on every load; either failure leaves the shipped class registered and reports
  the error on the card. The worst case is a tool that behaves as it always did.

Why this is allowed to claim a builtin's name when a user tool may not: the rule
:class:`~omnia.plugins.smart_notes.engine.tools.user_tools.UserToolLoader` enforces — a file may
only register its OWN name — exists so a tool from the ``user:`` namespace cannot silently shadow
``ai`` whatever its decorator says. Here, claiming that name IS the request, the file is in a
directory that exists for nothing else, and it was put there by a user who opened the builtin,
read its source and pressed Save. The same reasoning as user tools, applied to the same code the
add-on already runs: ``user_files/`` is preserved across updates and is never synced, so an
override on one computer cannot execute on another.

Pure logic — no ``aqt``/``anki`` imports.
"""

from __future__ import annotations

import inspect
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Optional

from omnia.plugins.smart_notes.engine.tools.registry import (
    get_tool,
    register_tool,
    registered_tools,
    unregister_tool,
)
from omnia.plugins.smart_notes.engine.tools.user_tools import (
    UserToolError,
    UserToolLoader,
    UserToolSource,
    UserToolStore,
    is_user_tool,
)

if TYPE_CHECKING:
    from omnia.plugins.smart_notes.engine.tools.base import Tool

#: The subdirectory of the tools folder overrides live in. A subdirectory rather than a suffix
#: convention, so :class:`UserToolStore`'s ``*.py`` glob cannot see them and try to load one as
#: a user tool (it would fail the name rule and report a tool the user never wrote).
OVERRIDE_DIRNAME = "builtin"

#: A builtin's registry name, which is also its file name here. Underscores are legal (the
#: shipped ``cloze_audio``) where a user-tool slug allows only dashes, which is exactly why this
#: is its own rule rather than :func:`~…user_tools.validate_slug`.
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


def validate_builtin_name(name: str) -> str:
    """Return ``name`` unchanged, or raise when it is not a legal builtin tool name.

    Args:
        name: The candidate name.

    Returns:
        The validated name.

    Raises:
        UserToolError: When it is empty or holds anything but lowercase letters, digits and
            underscores — which is also what keeps it a safe file name.
    """
    if not _NAME_RE.match(name or ""):
        raise UserToolError(
            f"{name!r} is not a builtin tool name — use lowercase letters, digits and "
            "underscores (1-40 characters)"
        )
    return name


def overridable_tools() -> list[str]:
    """The registered tools that can be edited: the builtins, in registry order.

    A ``user:`` tool is excluded because it already HAS an editor — its own file is the thing
    the Tools tab opens, and routing it through here would give one tool two homes on disk.
    """
    return [name for name in registered_tools() if not is_user_tool(name)]


def builtin_tool_source(name: str) -> str:
    """Return the Python of the tool currently REGISTERED under ``name``.

    Read from the class's own module rather than from a path this module builds, so it keeps
    working wherever the add-on is installed, and so what the editor shows is unarguably the
    code that is running — which is also why this says "registered" and not "shipped". With an
    override loaded, the registered class came from the override's file and that is what comes
    back; the caller reads the override's file directly anyway (it is the one that survives a
    file the loader could not compile), so the two agree.

    Args:
        name: The builtin's registry name.

    Returns:
        The module's full source text.

    Raises:
        UserToolError: When no such tool is registered, or its source cannot be read (a build
            that shipped only ``.pyc`` files, say).
    """
    cls = get_tool(validate_builtin_name(name))
    if cls is None:
        raise UserToolError(f"no tool called {name!r} is installed")
    module = sys.modules.get(getattr(cls, "__module__", "") or "")
    try:
        if module is None:
            raise UserToolError(f"{name!r} has no importable module to read")
        return inspect.getsource(module)
    except UserToolError:
        raise
    except Exception as exc:  # no source on disk, a frozen import, an unreadable file
        raise UserToolError(f"could not read the source of {name!r}: {exc}") from exc


#: The shipped classes overrides have displaced, by builtin name.
#:
#: MODULE scope, because what it shadows is module scope. Two loaders exist in a running Anki —
#: the plugin builds one at enable, the settings dialog builds another when it opens — and both
#: bind the SAME registry. Held per instance, the memory belonged to whichever loader displaced
#: the class first, so the OTHER one restored nothing: Restore built-in re-registered the
#: dialog's own copy of the user's class, and disabling the feature left an override saved in
#: this session still running. (``UserToolLoader.unload_all`` documents having hit the same
#: shape of bug from the other side, which is why it keys on the namespace rather than on its
#: own bookkeeping. An override has no namespace to key on — this dict is its equivalent.)
#:
#: Written only at the FIRST displacement of a name, so restoring lands on the class that
#: shipped rather than on an earlier override.
_SHIPPED: dict[str, Optional[type[Tool]]] = {}


def displaced_builtins() -> tuple[str, ...]:
    """The builtin names an override is currently loaded over, sorted."""
    return tuple(sorted(_SHIPPED))


class BuiltinOverrideStore(UserToolStore):
    """``user_files/tools/builtin/``: one file per overridden builtin, named after it.

    Same shape as the user-tool store — the directory IS the database, deleting the file removes
    the override — with the one difference that its file names are builtin names.
    """

    NOUN: ClassVar[str] = "edited builtin"

    def path_for(self, name: str) -> Path:
        """Return the file path for ``name`` (validated, so it cannot escape the directory)."""
        return self.directory / f"{validate_builtin_name(name)}.py"

    def slugs(self) -> list[str]:
        """Return the builtin names this directory holds a file for, sorted."""
        if not self.directory.is_dir():
            return []
        return sorted(
            path.stem
            for path in self.directory.glob("*.py")
            if _NAME_RE.match(path.stem)
        )


class BuiltinOverrideLoader(UserToolLoader):
    """Loads overrides over the shipped builtins, and can put the shipped ones back.

    Everything about compiling, guarding and isolating a bad file is inherited; what changes is
    the name a file claims (its own stem, not ``user:<stem>``) and what registering it displaces.
    """

    #: Its own ``sys.modules`` namespace, so an override OF ``cloze`` and a user tool CALLED
    #: ``cloze`` cannot land on the same key — which would make a class's ``__module__``, and
    #: therefore :func:`builtin_tool_source`, point at whichever loaded last.
    MODULE_PREFIX: ClassVar[str] = "omnia_builtin_override_"

    def name_for(self, slug: str) -> str:
        """An override file claims the builtin's own name — that is the whole point of it."""
        return validate_builtin_name(slug)

    def overridden(self) -> tuple[str, ...]:
        """The builtin names an override is currently loaded over (see :data:`_SHIPPED`).

        Deliberately NOT this loader's own ``loaded``: what the caller wants to know is whether
        the tool running under that name is the user's, and the dialog's loader and the
        plugin's each see only the files they themselves loaded.
        """
        return displaced_builtins()

    def unload_all(self) -> None:
        """Restore every displaced builtin (the plugin's disable teardown).

        Keyed on :data:`_SHIPPED` rather than on this loader's own bookkeeping, and not on the
        registry either: there is no prefix marking an override, so sweeping names would
        unregister builtins nothing ever touched. The shared dict is the only thing that knows
        which ones were displaced — including by a loader that is not this one, which is exactly
        the case that used to leave an override saved in the dialog running after the feature
        was switched off.
        """
        for name in displaced_builtins():
            self._unregister(name)
        self._loaded.clear()

    def _register(self, name: str, cls: type[Tool]) -> None:
        """Bind ``name`` to the override, remembering what it displaced."""
        if name not in _SHIPPED:
            _SHIPPED[name] = get_tool(name)
        unregister_tool(name)
        register_tool(name)(cls)
        self._loaded.add(name)

    def _unregister(self, name: str) -> None:
        """Drop the override and put the shipped class back.

        A name this loader never displaced is left ALONE — the registry entry under it is the
        builtin itself, and unregistering it would take the tool off the picker entirely. That
        is not hypothetical: restoring a builtin that had no override, or reloading a folder
        whose file was deleted by hand, both come through here.
        """
        self._loaded.discard(name)
        if name not in _SHIPPED:
            return
        shipped = _SHIPPED.pop(name)
        unregister_tool(name)
        if shipped is not None:
            register_tool(name)(shipped)

    def save(self, source: UserToolSource) -> type[Tool]:
        """Compile ``source``, then write and load it as ``source.slug``'s override.

        Compiling FIRST is the whole safety story: a file that cannot load is never written, so
        an edit that does not work leaves the builtin exactly as it was rather than taking it
        off the registry until the user finds the folder.

        Args:
            source: The builtin's name (as the slug) and the edited code.

        Returns:
            The registered override class.

        Raises:
            UserToolError: When the code does not compile, does not register the builtin's name,
                or cannot be written.
        """
        name = self.name_for(source.slug)
        cls = self.compile_tool(source, filename=str(self.store.path_for(name)))
        self.store.write(source)
        self._register(name, cls)
        return cls

    def restore(self, name: str) -> bool:
        """Delete ``name``'s override and put the shipped builtin back.

        Args:
            name: The builtin's registry name.

        Returns:
            Whether an override file existed.

        Raises:
            UserToolError: When the file exists but cannot be removed.
        """
        existed = self.store.delete(validate_builtin_name(name))
        self._unregister(name)
        return existed
