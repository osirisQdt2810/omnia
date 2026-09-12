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
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Optional

from omnia import addon_user_files_dir
from omnia.core import anki_compat
from omnia.core.logging import get_logger
from omnia.gui.smart_notes.dialogs.context import SmartNotesContext
from omnia.plugins.smart_notes.engine import LanguageDetector
from omnia.plugins.smart_notes.engine.tools import (
    INPUT_KIND_EXTENSIONS,
    TEXT_INPUT,
    ReviewGate,
    ToolContext,
    UserToolError,
    UserToolLoad,
    UserToolLoader,
    UserToolSource,
    UserToolStore,
    UserToolTester,
    declared_inputs,
    is_user_tool,
    registered_tools,
    risky_operations,
    slugify,
    user_tool_name,
    validate_slug,
)
from omnia.plugins.smart_notes.engine.tools.media_sample import (
    MediaSampleStage,
    image_data_uri,
    media_family,
    media_reference,
)

if TYPE_CHECKING:
    from omnia.plugins.smart_notes.config import ToolUsage
    from omnia.plugins.smart_notes.engine.tools import Tool, ToolTestResult

logger = get_logger("smart_notes")

#: Ceiling on a produced picture the page may inline as a ``data:`` URI. Base64 costs a third
#: again in size and the marshal runs on the Qt main thread, so a full-resolution render has to
#: be reported rather than shown. Audio and video are never inlined at any size — they go to
#: Anki's own player (see :meth:`UserToolsController.on_play_output`).
MAX_INLINE_PREVIEW_BYTES: Final = 8 * 1024 * 1024


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
        # Session-scoped, like the gate: closing the dialog disposes it, so a browsed file
        # never outlives the dialog that staged it.
        self._sample_stage = MediaSampleStage()
        self._tester = UserToolTester()
        # The bytes of the last MEDIA result, so the page's Play button has something to hand
        # Anki's player. Session-scoped for the same reason as the stage, and cleared by a text
        # result or an error so Play can never replay a run the user has moved on from.
        self._last_output: Optional[tuple[bytes, str]] = None

    def ops(self) -> dict[str, Callable[..., Any]]:
        """The ``{op_name: handler}`` map this controller owns."""
        return {
            "user_tools": self.on_list,
            "user_tool_generate": self.on_generate,
            "user_tool_test": self.on_test,
            "user_tool_save": self.on_save,
            "user_tool_delete": self.on_delete,
            "user_tool_open_dir": self.on_open_dir,
            "user_tool_pick_sample": self.on_pick_sample,
            "user_tool_inputs": self.on_inputs,
            "user_tool_play_output": self.on_play_output,
            "user_tool_risks": self.on_risks,
        }

    def on_risks(self, data: dict[str, Any]) -> dict[str, Any]:
        """Return what the CURRENT editor contents reach for.

        The source box is editable and paste-able, so the summary shipped with a tool goes
        stale the moment it is changed. Cheap enough to recompute on a debounce: it is one AST
        parse of a file a human is expected to read.
        """
        return {"risks": risky_operations(str(data.get("source", "")))}

    def on_inputs(self, data: dict[str, Any]) -> dict[str, Any]:
        """Return the inputs the CURRENT editor contents declare, so the page can build a form.

        This does NOT compile the draft, and that is the point: compiling means ``exec``, and in
        this flow arbitrary execution happens exactly once — on Run, after the risk banner has
        told the user what the code reaches for. The form is needed before Run, so it is read
        from the source text instead (see
        :func:`~omnia.plugins.smart_notes.engine.tools.user_tools.declared_inputs`). One AST
        parse, cheap enough to answer on a keystroke debounce, like ``user_tool_risks`` beside
        it — so this is synchronous and un-threaded too.
        """
        return {
            "inputs": [
                {"field": item.field, "kind": item.kind}
                for item in declared_inputs(str(data.get("source", "")))
            ]
        }

    def dispose(self) -> None:
        """Drop anything this controller staged. Called when the dialog closes.

        Must not raise: it runs from ``closeEvent``, where an exception would stop the dialog
        closing over a temp file nobody can see.
        """
        self._last_output = None
        try:
            self._sample_stage.dispose()
        except Exception:  # pragma: no cover - defensive
            logger.exception("smart_notes: could not clean up the sample stage")

    def on_pick_sample(self, data: dict[str, Any]) -> dict[str, Any]:
        """Let the user choose a FILE for ONE declared input, and return its reference.

        A tool that reads media needs a sample that resolves like a real note's does: the field
        holds a reference, and the file sits where ``ctx.media_dir()`` points. Typing
        ``[sound:x.mp3]`` by hand only works if x.mp3 is already in the collection, which is
        exactly the case a user testing a NEW conversion does not have.

        The picker is filtered to the kind that input declares and opens in the collection's
        media folder — where the interesting files already are — but accepts a file from
        anywhere. Whatever is chosen is staged OUTSIDE the collection (see
        :class:`~omnia.plugins.smart_notes.engine.tools.media_sample.MediaSampleStage` for why a
        test must not add to synced media), under this input's own slot, so another input's file
        stays staged.

        Any input can reach this, not only a declared media one: a tool whose ``input_kinds``
        the form could not read (absent, computed, or written before the declaration existed)
        renders a text row, and that row's own attach button lands here with ``kind="file"`` —
        unfiltered, because a draft that did not say what it reads cannot have a filter derived
        from it. That is what keeps such a tool testable without a standalone browse button
        sitting next to Run whatever the tool takes.

        Args:
            data: ``{"field": <the input's name>, "kind": <one of INPUT_KINDS>}``.

        Returns:
            ``{"reference": "<what to put in the input>", "name": "<staged file name>"}``, or
            ``{}`` when the user cancelled, or ``{"error": …}`` when the file cannot be staged.
        """
        field = str(data.get("field", "")).strip()
        kind = str(data.get("kind", TEXT_INPUT)) or TEXT_INPUT
        if not field:
            return {"error": "That input has no name, so a file has nowhere to go."}
        start = ""
        try:
            start = anki_compat.media_dir()
        except Exception:  # pragma: no cover - no collection is not an error here
            logger.debug("smart_notes: no media folder to start the picker in")
        path = anki_compat.pick_file(
            title=self._picker_title(kind, field),
            file_filter=self._qt_filter(kind),
            start_dir=start,
        )
        if not path:
            return {}
        try:
            name = self._sample_stage.stage(Path(path), slot=field)
        # Broad at the UI boundary: an unreadable file, a full disk and a permissions problem
        # all mean the same thing to the user, and none may take the tab down.
        except Exception as exc:
            logger.exception("smart_notes: could not stage the sample file %r", path)
            return {"error": f"Could not use that file ({exc})."}
        # From the FILE's extension, not from the declared kind: a "file" input holding a .png
        # still needs the <img> form, because that is what a real note would hold.
        return {"reference": media_reference(name), "name": name}

    @staticmethod
    def _picker_title(kind: str, field: str) -> str:
        """The picker's window title, naming the family only when there IS one.

        ``"file"`` (and anything unrecognised) has no filter, so calling it "a file file" would
        promise a filtering the dialog is not doing.
        """
        if INPUT_KIND_EXTENSIONS.get(kind):
            return f"Choose the {kind} file for {field}"
        return f"Choose a file for {field}"

    @staticmethod
    def _qt_filter(kind: str) -> str:
        """The Qt filter string for an input of ``kind``.

        Always ends in an All-files entry, including for a filtered kind: a filter that hides
        the file the user meant is worse than no filter at all (the same reasoning that leaves
        ``"file"`` with no extension list in the first place).
        """
        extensions = INPUT_KIND_EXTENSIONS.get(kind, ())
        if not extensions:
            return "All files (*)"
        patterns = " ".join(f"*.{extension}" for extension in extensions)
        return f"{kind.title()} files ({patterns});;All files (*)"

    def on_play_output(self, _data: dict[str, Any]) -> dict[str, Any]:
        """Play the last produced media file through Anki's own player.

        A *video* going through a function called ``play_audio`` is deliberate, not a mistake:
        ``aqt.sound.av_player`` is Anki's audio AND video player (it drives the bundled mpv),
        and it is the only thing on the machine that decodes H.264 — the dialog's webview does
        not, via ``data:``, ``blob:`` or ``http:`` alike. So the bytes go to the same player a
        card's ``[sound:clip.mp4]`` uses, and nothing is marshalled into the page.
        """
        if self._last_output is None:
            return {"error": "There is nothing to play — run the tool first."}
        data, ext = self._last_output
        try:
            anki_compat.play_audio(data, ext)
        # Broad at the UI boundary: a missing player, an unwritable temp dir and an unsupported
        # container all mean "it did not play" to the user, and none may take the tab down.
        except Exception as exc:
            logger.exception("smart_notes: could not play the tool's output")
            return {"error": f"Could not play it ({exc})."}
        return {"ok": True}

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
                    # Shipped with the source so opening an EXISTING tool for review shows its
                    # reach immediately. Without it the banner was empty over `import
                    # subprocess` — and an empty banner affirmatively means "only reshapes
                    # text", which is the opposite of true. The edit path ends in Run, which
                    # EXECUTES, so this is the same before-the-code-runs rule as generate.
                    "risks": risky_operations(source.code),
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
        inputs = self._posted_inputs(data)
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
                cls, inputs=inputs, params=params, ctx=self._tool_context()
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

    @staticmethod
    def _posted_inputs(data: dict[str, Any]) -> dict[str, str]:
        """Read the ``{field: value}`` map the Try-it form posted, defensively.

        A ``pycmd`` payload is untrusted input like any other boundary value, and the tester
        already has a defined behaviour for an empty map (it runs under the single sample
        field), so anything unreadable becomes one rather than an exception.
        """
        posted = data.get("inputs") or {}
        if not isinstance(posted, dict):
            return {}
        return {str(field): str(value) for field, value in posted.items()}

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
            # The SAME resolver generation uses. Without it a tool that reads media declined
            # on every Test — on a machine with the collection wide open — and the gate still
            # marked it tested, so Save unlocked for a tool the user never saw do its job.
            # The STAGE when the user picked a file, the real collection otherwise. This is
            # what lets a tool resolve `[sound:x]` for a file that is not in the collection —
            # without a test writing anything into synced media.
            media_dir=self._sample_media_dir,
        )

    def _sample_media_dir(self) -> str:
        """Where a TEST run resolves media — the stage, and never the live collection.

        A test executes arbitrary code that may now open files for writing, so pointing it at
        the real media folder makes Run itself destructive: a tool that writes its output back
        over its input truncates the user's own file, and the review gate REQUIRES pressing
        Run. The blast radius of a mistake has to be a temp copy.

        Testing against a file that IS in the collection still works, and is one click: the
        picker opens in the media folder, and what it returns is a COPY in the stage. Nothing
        the tool does can reach the original.

        Returns "" until something is staged, which a tool reading media treats as "no
        collection" and declines on — the same contract as a headless build.
        """
        return self._sample_stage.directory

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
        # The risks travel WITH the source. Computing them only after a test run put the
        # warning after the moment of risk: the user must press Run to satisfy the review gate,
        # so "this runs other programs on your computer" arriving in the Run RESULT is a
        # warning about something that already happened.
        result: dict[str, Any] = (
            {"error": error}
            if error
            else {"source": source, "risks": risky_operations(source)}
        )
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
            self._last_output = None
            payload: dict[str, Any] = {"error": error or "The tool returned no result."}
        else:
            self._gate.mark_tested(code)
            payload = {
                "ok": result.ok,
                "status": result.status,
                "output": result.output,
                "detail": result.detail,
                # What this tool reaches for, in the reader's words. The import allowlist is no
                # longer the boundary — this review is — and a review is only worth something
                # if the reader is told what to look for. Spotting a subprocess import on line
                # 3 of forty lines of generated Python, read once, is not a fair ask.
                "risks": risky_operations(code),
            }
            # A SIBLING key rather than a repurposed `output`: the text path and everything
            # reading it stay exactly as they were, and one key keeps meaning one thing.
            media = self._media_payload(slug, result)
            if media is not None:
                payload["media"] = media
        self._ctx.eval_js(
            f"window.__snUserToolTested({json.dumps(slug)}, {json.dumps(payload)});"
        )

    def _media_payload(
        self, slug: str, result: ToolTestResult
    ) -> Optional[dict[str, Any]]:
        """Describe a produced FILE for the page, and remember its bytes for Play.

        Branches on the produced EXTENSION rather than on the generation kind, which is what
        lets a tool declaring ``kind="tts"`` with an ``mp4`` extension render as a video without
        touching ``GENERATION_KINDS``, the pipeline or the field-type dropdown.

        Only a picture crosses into the page, and only under
        :data:`MAX_INLINE_PREVIEW_BYTES`. Audio and video never do: this webview cannot decode
        H.264 or AAC at all, so the honest render is a name and a button that hands the file to
        Anki's player.

        Args:
            slug: The tool being tested — the name shown is ``<slug>.<ext>``, since a produced
                file has no name of its own until it is written into a note.
            result: The outcome of the run.

        Returns:
            The ``media`` block, or None for a result that is not a produced file (which also
            clears the remembered bytes, so Play cannot replay a run the user has left behind).
        """
        if not result.data:
            self._last_output = None
            return None
        data = result.data
        ext = (result.ext or "").lower()
        self._last_output = (data, ext)
        family = media_family(ext)
        payload: dict[str, Any] = {
            "kind": family,
            "name": f"{slug}.{ext or 'bin'}",
            "ext": ext,
            "size": len(data),
            "playable": family != "image",
            "note": "",
        }
        if family == "image":
            if len(data) <= MAX_INLINE_PREVIEW_BYTES:
                payload["image"] = image_data_uri(data, ext)
            else:
                # Never a blank box: the name, the size and the reason are always present, so
                # "it produced nothing" is never the impression a large picture leaves.
                payload["note"] = (
                    f"{len(data)} bytes — too large to preview here. It is a real file; a "
                    "field generated with this tool will hold it."
                )
        return payload
