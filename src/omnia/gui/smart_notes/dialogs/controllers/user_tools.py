"""The Tools tab's controller: author, review, test, save and delete user-authored tools.

The Anki glue over
:mod:`omnia.plugins.smart_notes.engine.tools.user_tools` (the on-disk store + loader + the
review gate) and :class:`~omnia.plugins.smart_notes.authoring.tool_author.ToolAuthor` (the one
LLM call). The flow it enforces is the safety model, so it is worth stating in one place:

1. ``user_tool_generate`` runs the LLM OFF the Qt thread and pushes the FULL generated source
   back to the page — the dialog shows it unedited, never a summary.
2. ``user_tool_test`` compiles that exact source and runs it once on the user's sample value,
   also off-thread (a user tool is arbitrary Python; the main thread must not wait on it).
3. ``user_tool_save`` REFUSES unless that exact source has been through step 2 in this session
   (:class:`~omnia.plugins.smart_notes.engine.tools.user_tools.ReviewGate`). The disabled Save
   button in the page is a courtesy; this is the rule.
4. ``user_tool_delete`` first reports which fields reference the tool and only deletes once the
   page confirms.

Only loaded inside Anki.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Optional

from omnia import addon_user_files_dir
from omnia.core import anki_compat
from omnia.core.logging import get_logger
from omnia.gui.smart_notes.dialogs.context import SmartNotesContext
from omnia.plugins.smart_notes.engine import LanguageDetector
from omnia.plugins.smart_notes.engine.tools import (
    ReviewGate,
    ToolContext,
    UserToolError,
    UserToolLoad,
    UserToolLoader,
    UserToolSource,
    UserToolStore,
    UserToolTester,
    is_user_tool,
    registered_tools,
    slugify,
    user_tool_name,
    validate_slug,
)

if TYPE_CHECKING:
    from omnia.plugins.smart_notes.config import ToolUsage
    from omnia.plugins.smart_notes.engine.tools import Tool, ToolTestResult

logger = get_logger("smart_notes")


class UserToolsController:
    """Tools tab ops: list / generate / test / save / delete user-authored tools.

    Args:
        ctx: The shared service context (the provider hub for the one LLM call, the page push,
            and the settings the delete warning is computed from).
        loader: The user-tool loader, injected so tests run against a ``tmp_path`` store.
            Defaults to the same ``user_files/tools`` directory the plugin loads at startup, so
            a tool saved here is live in the picker without restarting Anki.
    """

    def __init__(
        self, ctx: SmartNotesContext, loader: Optional[UserToolLoader] = None
    ) -> None:
        self._ctx = ctx
        self._loader = (
            loader
            if loader is not None
            else UserToolLoader(UserToolStore(addon_user_files_dir() / "tools"))
        )
        self._gate = ReviewGate()
        self._tester = UserToolTester()

    def ops(self) -> dict[str, Callable[..., Any]]:
        """The ``{op_name: handler}`` map this controller owns."""
        return {
            "user_tools": self.on_list,
            "user_tool_generate": self.on_generate,
            "user_tool_test": self.on_test,
            "user_tool_save": self.on_save,
            "user_tool_delete": self.on_delete,
            "user_tool_open_dir": self.on_open_dir,
        }

    def on_open_dir(self, _data: dict[str, Any]) -> dict[str, Any]:
        """Open the tools folder in the OS file manager.

        The whole point of the button: the folder is where the user's tools LIVE, and telling
        them a path they then have to retype into Finder/Explorer is not a location, it is
        homework. Qt opens it natively on every platform, so nothing here knows about macOS.
        """
        directory = self._loader.store.directory
        try:
            directory.mkdir(parents=True, exist_ok=True)
            anki_compat.open_local_path(directory)
        # Broad at the UI boundary: what a failed open raises is platform- and Qt-dependent,
        # and a folder that will not open must not take the Tools tab down with it.
        except Exception as exc:
            logger.exception("smart_notes: could not open the user tools folder")
            return {"error": f"Could not open {directory} ({exc})."}
        return {"ok": True}

    def ensure_loaded(self) -> None:
        """(Re)load every tool file into the registry — called before the catalog is baked.

        Idempotent, and cheap enough to run on every dialog open: it is what makes a tool the
        user authored in this dialog appear in the per-row picker of the SAME session, and what
        picks up a file dropped into the folder by hand.
        """
        self._load_all()

    def _load_all(self) -> list[UserToolLoad]:
        """Reload every tool file, returning one outcome per file ([] if the folder is broken).

        The loader already isolates one bad FILE; this guard covers the folder itself (an
        unreadable directory, a permissions problem) so neither the dialog nor the tab dies for
        an optional feature.
        """
        try:
            return self._loader.load_all()
        except (
            Exception
        ):  # boundary: the dialog must open even with a broken tools folder
            logger.exception("smart_notes: could not load user tools for the dialog")
            return []

    # -- ops -----------------------------------------------------------------------------

    def on_list(self, _data: dict[str, Any]) -> dict[str, Any]:
        """The Tools tab payload: the builtin tools, the user's own, and where they live.

        Reloads from disk on every open (the tab is where a tool is edited, and the folder is a
        folder the user may also edit by hand), so each card can also report the file's own load
        error instead of silently vanishing from the list.
        """
        loads = {load.slug: load for load in self._load_all()}
        tools: list[dict[str, Any]] = []
        for source in self._loader.store.list():
            load = loads.get(source.slug)
            tools.append(
                {
                    "slug": source.slug,
                    "name": source.name,
                    "prompt": source.prompt,
                    "source": source.code,
                    "error": "" if load is None else load.error,
                    **self._described(source.name),
                }
            )
        return {
            "tools": tools,
            "builtins": [
                self._described(name)
                for name in registered_tools()
                if not is_user_tool(name)
            ],
            # Two forms on purpose. `directory` is the absolute path — correct on every
            # platform because it is derived from the installed package's own location, never
            # written down — but it is long, and inlining it in a sentence made a runtime value
            # read as a hardcoded macOS literal. `directory_label` is the short, stable name to
            # show; the absolute path rides along for the tooltip and the Open-folder button.
            "directory": str(self._loader.store.directory),
            "directory_label": self._directory_label(),
        }

    def _directory_label(self) -> str:
        """The tools folder as ``user_files/tools``-style text, whatever the platform.

        Relative to the add-on root when it sits underneath it (always, in a real install);
        the absolute path is the honest fallback for a layout that does not.
        """
        directory = self._loader.store.directory
        try:
            return str(directory.relative_to(addon_user_files_dir().parent))
        except ValueError:
            return str(directory)

    def on_generate(self, data: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Write a tool's source from the user's description, OFF the Qt main thread.

        Returns an error payload synchronously for anything decidable here (a name that is not
        a usable slug, a broken provider config); otherwise returns None and pushes the source —
        or the failure — through ``window.__snUserToolSource``.
        """
        description = str(data.get("prompt", ""))
        try:
            slug = self._slug_from(data)
        except UserToolError as exc:
            return {"error": str(exc)}
        hub = self._ctx.build_hub()
        if hub is None:
            return {"error": "Provider config error — see logs."}
        fields = [str(name) for name in data.get("all_fields", [])]

        from omnia.plugins.smart_notes.authoring import ToolAuthor

        author = ToolAuthor(hub.llm())
        anki_compat.run_in_background(
            lambda: author.generate(slug, description, fields),
            on_success=lambda code: self._push_source(slug, source=code),
            on_failure=lambda exc: self._push_source(
                slug, error=self._ctx.friendly(exc, "Could not write the tool")
            ),
            label="Omnia: writing the tool…",
        )
        return None

    def on_test(self, data: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Compile the posted source and run it once on the user's sample, OFF the main thread.

        Off-thread because a user tool is arbitrary Python: however well-behaved the generated
        code looks, the Qt main thread must not be the one to find out. The gate is marked from
        the success callback whenever the tool actually RAN — a decline or even a raise is a
        result the user saw, and saving a tool that declines is their call to make.
        """
        code = str(data.get("source", ""))
        sample = str(data.get("sample", ""))
        params = dict(data.get("params", {}) or {})
        try:
            slug = self._slug_from(data)
        except UserToolError as exc:
            return {"error": str(exc)}
        if not code.strip():
            return {"error": "Generate (or paste) the tool's code first."}

        def work() -> ToolTestResult:
            cls = self._loader.compile_tool(UserToolSource(slug=slug, code=code))
            return self._tester.run(
                cls, sample=sample, params=params, ctx=self._tool_context()
            )

        anki_compat.run_in_background(
            work,
            on_success=lambda result: self._push_test(slug, code, result=result),
            on_failure=lambda exc: self._push_test(
                slug, code, error=self._ctx.friendly(exc, "The tool could not run")
            ),
            label="Omnia: running the tool…",
        )
        return None

    def on_save(self, data: dict[str, Any]) -> dict[str, Any]:
        """Write the reviewed source to ``user_files/tools/<slug>.py`` and register it.

        Refuses when this exact source has not been test-run in this session — the one rule that
        makes "an LLM wrote this and it can do anything the add-on can" acceptable.
        """
        code = str(data.get("source", ""))
        try:
            slug = self._slug_from(data)
        except UserToolError as exc:
            return {"error": str(exc)}
        if not self._gate.is_tested(code):
            return {
                "error": "Run the tool on a sample first — it is saved only after you have "
                "seen it work."
            }
        source = UserToolSource(
            slug=slug, code=code, prompt=str(data.get("prompt", ""))
        )
        try:
            self._loader.store.write(source)
        except UserToolError as exc:
            return {"error": str(exc)}
        load = self._loader.load(slug)
        if not load.ok:
            return {"error": f"Saved, but it could not be loaded: {load.error}"}
        return {"ok": True, "slug": slug, "name": source.name}

    def on_delete(self, data: dict[str, Any]) -> dict[str, Any]:
        """Delete a user tool — reporting which fields use it first, unless already confirmed.

        Two-phase on purpose: the first call answers "what would this affect?" so the page can
        name the fields, and only a ``confirm`` call removes the file. Nothing breaks for a
        field left pointing at the deleted name — the pipeline records ``unknown_tool`` and
        tries the next tool — but the user deserves to know which cards change.
        """
        try:
            slug = self._slug_from(data)
        except UserToolError as exc:
            return {"error": str(exc)}
        usages = [str(usage) for usage in self._settings_usages(user_tool_name(slug))]
        if usages and not data.get("confirm"):
            return {"usages": usages, "slug": slug}
        try:
            existed = self._loader.store.delete(slug)
        except UserToolError as exc:
            return {"error": str(exc)}
        self._loader.load_all()  # drops the registration of the file that just disappeared
        return {"ok": True, "slug": slug, "deleted": existed, "usages": usages}

    # -- helpers -------------------------------------------------------------------------

    def _slug_from(self, data: dict[str, Any]) -> str:
        """Resolve the posted ``slug`` (or derive one from ``label``), validated.

        Raises:
            UserToolError: When neither yields a usable slug.
        """
        raw = str(data.get("slug", "")).strip() or slugify(str(data.get("label", "")))
        return validate_slug(raw)

    def _settings_usages(self, name: str) -> list[ToolUsage]:
        """The fields whose chain references ``name`` (empty when the settings can't be read)."""
        try:
            return self._ctx.settings().fields_using_tool(name)
        except Exception:  # a delete must not depend on a readable settings blob
            logger.exception("smart_notes: could not check which fields use %r", name)
            return []

    def _tool_context(self) -> ToolContext:
        """The context a test run gets: the real hub, no language detector, a real logger.

        A user tool is deterministic by contract and should never touch ``providers`` — but the
        pipeline hands every tool the same context, so the test must too, or the test would be
        checking a tool that cannot exist. ``build_hub`` returning None is not fatal here for
        the same reason: a deterministic tool does not need it.
        """
        return ToolContext(
            providers=self._ctx.build_hub(),
            detector=LanguageDetector(enabled=False),
            logger=logger,
        )

    @staticmethod
    def _described(name: str) -> dict[str, Any]:
        """Describe a REGISTERED tool for the tab (label/kinds/params), or say it is not loaded.

        Read defensively and per tool: a user tool is code from outside this repo, so a class
        that fails to describe itself must cost only its own card.
        """
        from omnia.plugins.smart_notes.engine.tools import get_tool

        cls: Optional[type[Tool]] = get_tool(name)
        if cls is None:
            return {"name": name, "label": name, "description": "", "loaded": False}
        try:
            return {
                "name": name,
                "label": cls.label,
                "description": cls.description,
                "kinds": sorted(cls.kinds),
                "deterministic": cls.deterministic,
                "params_schema": (
                    None if cls.params_model is None else cls.params_model.schema()
                ),
                "loaded": True,
            }
        except Exception:
            logger.exception("smart_notes: tool %r could not describe itself", name)
            return {"name": name, "label": name, "description": "", "loaded": True}

    # -- page pushes ---------------------------------------------------------------------

    def _push_source(self, slug: str, *, source: str = "", error: str = "") -> None:
        """Send a generated tool source to ``window.__snUserToolSource`` (main thread)."""
        result: dict[str, Any] = {"error": error} if error else {"source": source}
        self._ctx.eval_js(
            f"window.__snUserToolSource({json.dumps(slug)}, {json.dumps(result)});"
        )

    def _push_test(
        self,
        slug: str,
        code: str,
        *,
        result: Optional[ToolTestResult] = None,
        error: str = "",
    ) -> None:
        """Send a test-run outcome to ``window.__snUserToolTested`` and arm the review gate.

        Runs on the Qt main thread (the ``run_in_background`` success/failure callback), which
        is the only place ``eval_js`` is safe — and the only place the gate may be marked, since
        "the user has seen a result" is exactly what having reached here means.
        """
        if error or result is None:
            payload: dict[str, Any] = {"error": error or "The tool returned no result."}
        else:
            self._gate.mark_tested(code)
            payload = {
                "ok": result.ok,
                "status": result.status,
                "output": result.output,
                "detail": result.detail,
            }
        self._ctx.eval_js(
            f"window.__snUserToolTested({json.dumps(slug)}, {json.dumps(payload)});"
        )
