"""User-authored tools: real Python :class:`Tool` subclasses that live on THIS device only.

A user describes a transform once ("from the audio filename, take the extension"), an LLM
writes a complete ``Tool`` subclass, and from then on the tool runs deterministically from the
same picker as ``ai``/``cloze``/``cloze_audio`` — with **no LLM call at run time**, which is the
entire point: a transform that needs no model should never cost tokens.

Everything here is the machinery around that: the on-disk home (:class:`UserToolStore`), the
loader that imports each file so its ``@register_tool("user:<slug>")`` runs
(:class:`UserToolLoader`), the review gate the authoring dialog cannot save past
(:class:`ReviewGate`) and the harness the dialog's test box runs a candidate through
(:class:`UserToolTester`). Pure logic — no ``aqt``/``anki`` imports, so all of it unit-tests
headless.

Executing code the user did not type — the honest version
--------------------------------------------------------

**What a user tool can reach: everything this add-on can.** The file is compiled and executed
inside Anki's own interpreter, in-process, on the same thread the rest of generation runs on.
Nothing here is a sandbox and nothing here pretends to be one: a user tool could read the
collection, touch the filesystem, open a socket, or read the API keys in ``user_files``. It
runs at exactly the add-on's trust level, because that is the only level Python offers without
a real isolation boundary (a separate process with dropped privileges), which this feature does
not have.

**Why that is acceptable for THIS path.** The code only ever arrives one way, and every step of
that way is a control:

* The user wrote the description that produced it, so its purpose is theirs.
* The authoring dialog shows the **complete, unedited source** — not a summary, not a diff.
* Save is refused until the user has actually **run** the tool once and seen its output
  (:class:`ReviewGate`); "generate and save" is not a reachable state.
* The result is a plain ``.py`` file in ``user_files/tools/`` — on that machine, in a directory
  the user can open, read, edit and delete with any editor.
* ``user_files/`` is Anki's preserved-on-update convention and is **NOT synced by AnkiWeb**, so
  approving code on one computer cannot execute it on another. A field chain persists only the
  tool NAME, so a device lacking the file resolves the entry to ``unknown_tool`` and the chain
  falls through — the same graceful degradation an uninstalled builtin already gets.

**What would NOT be acceptable, and is deliberately absent.** Generating and saving without the
review + test gate. Importing a tool from a URL, a shared deck, or any other user's machine.
Putting the SOURCE in the synced collection blob (the design this replaced, and the reason it
was replaced: one approval would then execute on every device the collection reaches). Running
a user tool's code at any time other than the import that registers it and the pipeline turn it
was configured for.

**A user tool now runs concurrently with itself, and nothing can stop it misbehaving.** With
bounded concurrency, up to ``max_concurrent_generations`` invocations of the same tool run at
once, for different notes. The engine protects the tool's inputs (a frozen read-only field map,
a fresh instance per resolve) and can protect nothing about its side effects — and the allowlist
deliberately permits ``subprocess``, ``shutil``, ``tempfile`` and ``os``, so side effects are
exactly what these tools have. A tool written against the old sequential engine that names a
scratch file after the field rather than the note will race itself and put one note's audio in
another note's field, with no error anywhere. The contract is stated in :class:`Tool`'s
docstring and in the authoring system prompt (rule 8b); a user who hand-edits a tool file is
the one case nothing enforces it for, which is why it is stated here too. Someone hitting this
can set ``max_concurrent_generations`` to 1.

**The import allowlist is a speed bump, not a boundary.** :class:`ImportGuard` refuses a module
whose ``import`` statements reach outside a small allowlist, and flags a handful of builtin
calls (``open``, ``eval``, ``__import__``, …). That catches the realistic failure here — a
generated tool that reaches for ``os.path`` or ``urllib.request`` because the model ignored its
instructions — and it makes the source the user reads match what the tool actually does. It
stops nobody who is trying: ``__import__("os")`` spelled through ``getattr`` on a module the
allowlist permits defeats it in one line. Treat it as a lint, never as a security control.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, Final, Optional

from omnia.core.logging import get_logger
from omnia.plugins.smart_notes.config import SmartNotesFieldRule
from omnia.plugins.smart_notes.engine.tools.base import (
    INPUT_KINDS,
    TEXT_INPUT,
    Empty,
    NotApplicable,
    Produced,
    Tool,
    ToolError,
    ToolRequest,
)
from omnia.plugins.smart_notes.engine.tools.registry import (
    TOOL_REGISTRY,
    register_tool,
    registered_tools,
    unregister_tool,
)

if TYPE_CHECKING:
    import logging
    from collections.abc import Mapping

    from omnia.plugins.smart_notes.engine.tools.base import ToolContext

#: The namespace every user-authored tool lives in. A builtin is a bare slug (``"cloze"``), so
#: a user tool can never shadow one however it is named or hand-edited.
USER_TOOL_PREFIX = "user:"

#: What a slug may be. Kept to lowercase/digits/dashes so it is also a safe FILE name — the
#: store resolves ``<slug>.py`` inside its directory, and this is what makes that resolution
#: incapable of escaping it (no dots, no separators, no ``..``).
_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")

#: The one-line JSON header a saved tool carries, holding the authoring prompt so "Regenerate"
#: can re-run it. A comment (not a docstring) so it survives however the module body is edited,
#: and so reading it never means executing the file.
_HEADER_PREFIX = "# omnia-user-tool: "

#: ``sys.modules`` key for a loaded tool. Registered before exec because pydantic v1 resolves a
#: model's annotations through ``sys.modules[cls.__module__].__dict__`` at class-creation time.
_MODULE_PREFIX = "omnia_user_tool_"

GENERATION_KINDS = ("text", "image", "tts")

#: The field name the Try-it form falls back to when a draft declares no inputs of its own.
#: Also the rule's base field and its only prompt ref in that case, so a tool whose
#: ``<something>_field`` param is blank still finds the typed value through the same defaulting
#: the builtins use.
SAMPLE_FIELD: Final = "Sample"

logger = get_logger("smart_notes")


class UserToolError(ToolError):
    """A user tool could not be read, checked, compiled, or registered.

    A :class:`~omnia.plugins.smart_notes.engine.tools.base.ToolError` (hence a
    :class:`~omnia.core.providers.errors.ProviderError`), so the dialog prints its message
    verbatim the way it already prints a provider failure — these messages are written FOR the
    user ("this tool imports 'os', which user tools may not use").
    """


def user_tool_name(slug: str) -> str:
    """Return the registry name for ``slug`` (``"extract-ext"`` → ``"user:extract-ext"``)."""
    return f"{USER_TOOL_PREFIX}{slug}"


def is_user_tool(name: str) -> bool:
    """Return whether ``name`` is in the user namespace (rather than a builtin's bare slug)."""
    return name.startswith(USER_TOOL_PREFIX)


def slugify(text: str) -> str:
    """Turn a human name ("Extract extension") into a slug ("extract-extension").

    Best-effort and lossy on purpose: anything outside ``[a-z0-9]`` becomes a dash, runs
    collapse, and the result is trimmed to the 40 characters :func:`validate_slug` allows. The
    caller still validates — an all-punctuation name yields ``""``, which is rejected there
    with a message rather than silently turned into a file called ``-.py``.

    Args:
        text: The name the user typed.

    Returns:
        The slug, or ``""`` when ``text`` carries no usable characters.
    """
    lowered = re.sub(r"[^a-z0-9]+", "-", text.strip().lower())
    return lowered.strip("-")[:40].strip("-")


def validate_slug(slug: str) -> str:
    """Return ``slug`` unchanged, or raise when it is not a legal tool slug.

    Args:
        slug: The candidate slug.

    Returns:
        The validated slug.

    Raises:
        UserToolError: When it is empty or contains anything but ``a-z``, ``0-9`` and inner
            dashes — which is also what keeps it a safe file name.
    """
    if not _SLUG_RE.match(slug or ""):
        raise UserToolError(
            f"{slug!r} is not a usable tool name — use lowercase letters, digits and dashes "
            "(1-40 characters)"
        )
    return slug


def source_fingerprint(code: str) -> str:
    """Return a short stable digest of ``code`` (what :class:`ReviewGate` remembers)."""
    return hashlib.sha256(code.encode("utf-8", "replace")).hexdigest()


@dataclass(frozen=True)
class UserToolSource:
    """One user tool as it exists on disk: its slug, its Python, and the prompt that wrote it.

    The prompt travels IN the file (a one-line JSON header comment) rather than in config: a
    tool is a single self-contained artefact the user can copy, read, or delete with an editor,
    and nothing about it reaches the synced collection.
    """

    slug: str
    code: str
    prompt: str = ""

    @property
    def name(self) -> str:
        """The registry name this tool registers under (``user:<slug>``)."""
        return user_tool_name(self.slug)

    def render(self) -> str:
        """Return the file text: the metadata header, then the code (header-free).

        Re-rendering an already-saved tool must not stack headers, so any leading header line
        in :attr:`code` is dropped first.
        """
        header = _HEADER_PREFIX + json.dumps(
            {"prompt": self.prompt}, ensure_ascii=False
        )
        return f"{header}\n{_strip_header(self.code).lstrip()}"

    @classmethod
    def parse(cls, slug: str, text: str) -> UserToolSource:
        """Build a source object from a file's text, reading its header when it has one.

        A file with no (or a malformed) header is still a valid tool — it simply has no stored
        prompt, so "Regenerate from prompt" asks the user for one. Hand-written files are
        first-class here for exactly that reason.

        Args:
            slug: The tool's slug (from the file name).
            text: The file's full text.

        Returns:
            The parsed source.
        """
        return cls(
            slug=slug, code=_strip_header(text).lstrip(), prompt=_parse_header(text)
        )


def _strip_header(text: str) -> str:
    """Return ``text`` without its leading metadata header line, if it has one."""
    if text.startswith(_HEADER_PREFIX):
        _, _, rest = text.partition("\n")
        return rest
    return text


def _parse_header(text: str) -> str:
    """Return the authoring prompt stored in ``text``'s header ("" when absent/malformed)."""
    if not text.startswith(_HEADER_PREFIX):
        return ""
    line = text.split("\n", 1)[0][len(_HEADER_PREFIX) :]
    try:
        data = json.loads(line)
    except ValueError:
        # A hand-edited header is not a reason to refuse the tool — it only costs the prompt.
        logger.warning("smart_notes: ignoring an unreadable user-tool header")
        return ""
    return str(data.get("prompt", "")) if isinstance(data, dict) else ""


class ImportGuard:
    """Refuses a user-tool module that reaches outside an allowlist of imports.

    A **speed bump, not a sandbox** — and since the allowlist now includes ``os``,
    ``subprocess`` and the filesystem, it is worth being exact about what that means.

    A user tool is arbitrary Python that runs in Anki's own process. Anki add-ons already have
    unrestricted Python, and this dialog has always said so ("Everything here runs with the
    same access as the add-on itself"), so the guard was never the thing standing between a
    tool and the machine — the mandatory read-and-run review is. Keeping the list narrow while
    the surrounding reality was wide bought no safety and cost the feature its point: a
    transform that CONVERTS rather than rewrites cannot be written without touching a file, so
    the model produced tools that renamed a string and looked like they worked.

    What the guard still buys, and why it is kept:

    * **the source you read is the source that runs.** An import outside the list is refused at
      load, so a tool cannot quietly grow a capability between the review and the next run;
    * **the review can be informed.** :func:`risky_operations` reads the same module and names
      what it reaches for, so the dialog can show "this writes files and runs programs" above
      the code instead of leaving the reader to spot it.

    What it does NOT buy: containment. A tool that imports ``subprocess`` can do whatever the
    user running Anki can do. That is the user's decision, taken deliberately, and the reason
    nothing is saved until the code has been read and test-run.
    """

    #: Dotted prefixes a user tool may import. ``"urllib.parse"`` is listed in full: importing
    #: it does NOT make ``urllib.request`` available, so the narrower entry is the honest one.
    ALLOWED_MODULES: frozenset[str] = frozenset(
        {
            "__future__",
            "base64",
            "collections",
            "dataclasses",
            "datetime",
            "decimal",
            "difflib",
            "functools",
            "html",
            "itertools",
            "json",
            "math",
            "re",
            "string",
            "textwrap",
            "typing",
            "unicodedata",
            "urllib.parse",
            # --- Files, processes and the media a tool may need to touch --------------------
            # Opened deliberately (see the class docstring). A transform that CONVERTS rather
            # than rewrites — pull the audio out of a video, resize a picture — has to reach
            # the file the note refers to, and everything short of that produced tools that
            # renamed a string and pretended.
            "io",
            "os",
            "pathlib",
            "shutil",
            "subprocess",
            "tempfile",
            "wave",
            "hashlib",
            "mimetypes",
            "uuid",
            # The media folder's location and the audio runtime, so a tool does not have to
            # guess either.
            "omnia.core.audio",
            "pydantic",
            "omnia.core.config.base",
            "omnia.core.lang",
            "omnia.core.lang.text",
            "omnia.plugins.smart_notes.config",
            # The whole ENGINE package: the Tool contract, the outcome types, the result
            # dataclass and the pure helpers a tool defaults its params from
            # (``rules.rule_source_fields``). Everything under it is pure logic by the repo's
            # coupling rule — no ``aqt``/``anki`` — which is exactly the line being drawn here.
            "omnia.plugins.smart_notes.engine",
        }
    )

    #: Builtins that build and run code from a string. These stay REFUSED even now that the
    #: filesystem is allowed, and for a different reason: they defeat the one guarantee the
    #: guard still makes — that the source the user read is the source that runs. ``open`` is
    #: deliberately not here any more; a tool that converts a file has to open it, and the
    #: review is told so by :func:`risky_operations`.
    FLAGGED_CALLS: frozenset[str] = frozenset(
        {
            "__import__",
            "breakpoint",
            "compile",
            "eval",
            "exec",
            "globals",
            "input",
        }
    )

    def check(self, code: str) -> None:
        """Parse ``code`` and raise when it imports or calls something out of bounds.

        Args:
            code: The module source.

        Raises:
            UserToolError: On a syntax error (reported with its line), a disallowed import, or
                a flagged builtin call.
        """
        try:
            tree = ast.parse(code)
        except SyntaxError as exc:
            raise UserToolError(
                f"the tool has a syntax error on line {exc.lineno}: {exc.msg}"
            ) from exc
        for node in ast.walk(tree):
            self._check_node(node)

    def _check_node(self, node: ast.AST) -> None:
        """Raise when one AST node is a disallowed import or a flagged builtin call."""
        if isinstance(node, ast.Import):
            for alias in node.names:
                self._check_module(alias.name)
        elif isinstance(node, ast.ImportFrom):
            # A relative import (level > 0) has no package to resolve against here.
            if node.level:
                raise UserToolError("a user tool may not use a relative import")
            self._check_module(node.module or "")
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in self.FLAGGED_CALLS
        ):
            raise UserToolError(
                f"a user tool may not call {node.func.id}() — it transforms the note's "
                "fields and nothing else"
            )

    def _check_module(self, module: str) -> None:
        """Raise unless ``module`` is (or is inside) an allowed module."""
        parts = module.split(".")
        prefixes = {".".join(parts[: index + 1]) for index in range(len(parts))}
        if not prefixes & self.ALLOWED_MODULES:
            raise UserToolError(
                f"a user tool may not import {module!r} — allowed: "
                + ", ".join(sorted(self.ALLOWED_MODULES))
            )


class UserToolStore:
    """Owns ``user_files/tools/``: the on-disk home of every user-authored tool.

    One file per tool (``<slug>.py``), and the directory is the whole database — there is no
    index to keep in step, deleting the file deletes the tool, and none of it reaches the synced
    collection. The directory is injected (DIP) so the tests run against ``tmp_path`` and never
    write Python into the repo.
    """

    def __init__(self, directory: Path) -> None:
        self._directory = Path(directory)

    @property
    def directory(self) -> Path:
        """The directory holding the tool files (created on first write)."""
        return self._directory

    def path_for(self, slug: str) -> Path:
        """Return the file path for ``slug`` (validated, so it cannot escape the directory)."""
        return self._directory / f"{validate_slug(slug)}.py"

    def slugs(self) -> list[str]:
        """Return the slugs of every ``.py`` file in the directory, sorted.

        Files whose name is not a legal slug (``__init__.py``, an editor backup) are skipped
        rather than reported: the directory is a plain folder the user can put anything in.
        """
        if not self._directory.is_dir():
            return []
        return sorted(
            path.stem
            for path in self._directory.glob("*.py")
            if _SLUG_RE.match(path.stem)
        )

    def read(self, slug: str) -> Optional[UserToolSource]:
        """Return the stored tool for ``slug``, or None when there is no such file.

        Raises:
            UserToolError: When the file exists but cannot be read as text.
        """
        path = self.path_for(slug)
        if not path.is_file():
            return None
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise UserToolError(f"could not read {path.name}: {exc}") from exc
        return UserToolSource.parse(slug, text)

    def list(self) -> list[UserToolSource]:
        """Return every readable tool in the directory, in slug order.

        An unreadable file is logged and skipped — one broken file must not empty the Tools tab.
        """
        sources: list[UserToolSource] = []
        for slug in self.slugs():
            try:
                source = self.read(slug)
            except UserToolError:
                logger.exception("smart_notes: could not read the user tool %r", slug)
                continue
            if source is not None:
                sources.append(source)
        return sources

    def write(self, source: UserToolSource) -> Path:
        """Persist ``source`` as ``<slug>.py`` (creating the directory), returning its path.

        Raises:
            UserToolError: When the file cannot be written.
        """
        path = self.path_for(source.slug)
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            path.write_text(source.render(), encoding="utf-8")
        except OSError as exc:
            raise UserToolError(f"could not save {path.name}: {exc}") from exc
        return path

    def delete(self, slug: str) -> bool:
        """Delete ``slug``'s file, returning whether it existed.

        Raises:
            UserToolError: When the file exists but cannot be removed.
        """
        path = self.path_for(slug)
        if not path.is_file():
            return False
        try:
            path.unlink()
        except OSError as exc:
            raise UserToolError(f"could not delete {path.name}: {exc}") from exc
        return True


@dataclass(frozen=True)
class UserToolLoad:
    """The outcome of loading ONE user tool file."""

    slug: str
    error: str = ""

    @property
    def ok(self) -> bool:
        """Whether the tool compiled and registered."""
        return not self.error

    @property
    def name(self) -> str:
        """The registry name the file was loaded under."""
        return user_tool_name(self.slug)


class UserToolLoader:
    """Compiles the files in a :class:`UserToolStore` and registers them as tools.

    Two rules make loading safe to run at plugin start:

    * **One bad file costs only itself.** Every file is compiled and registered inside its own
      guard, so a module that raises at import is logged and SKIPPED — startup continues and
      every other tool still loads. (The rest of the seam already assumes this: the registry's
      catalog/field-derivation walks and the pipeline's attempt guard each isolate one tool.)
    * **A file may only claim its own name.** The registry is snapshotted around the exec, and
      the module must have registered exactly ``user:<slug>`` — nothing else. A file that tries
      to register ``ai`` (or a second name) is rolled back and reported, so a user tool can
      never shadow a builtin whatever its ``@register_tool`` argument says.
    """

    def __init__(
        self,
        store: UserToolStore,
        *,
        guard: Optional[ImportGuard] = None,
        log: Optional[logging.Logger] = None,
    ) -> None:
        self._store = store
        self._guard = guard if guard is not None else ImportGuard()
        self._log = log if log is not None else logger
        self._loaded: set[str] = set()

    @property
    def store(self) -> UserToolStore:
        """The store this loader reads from."""
        return self._store

    @property
    def loaded(self) -> tuple[str, ...]:
        """The registry names this loader currently has registered, sorted."""
        return tuple(sorted(self._loaded))

    def load_all(self) -> list[UserToolLoad]:
        """Load every tool file in the store, skipping (and logging) the ones that break.

        Idempotent: re-running it re-compiles each file and rebinds its name, so a tool the user
        just saved or edited takes effect without restarting Anki. Names whose file has since
        disappeared are unregistered.

        Returns:
            One :class:`UserToolLoad` per file, in slug order.
        """
        slugs = self._store.slugs()
        for name in tuple(self._loaded):
            if name[len(USER_TOOL_PREFIX) :] not in slugs:
                self._unregister(name)
        return [self.load(slug) for slug in slugs]

    def load(self, slug: str) -> UserToolLoad:
        """Load ONE tool file and register it, returning the outcome instead of raising.

        Args:
            slug: The tool's slug.

        Returns:
            A successful :class:`UserToolLoad`, or one carrying the user-facing failure reason.
        """
        try:
            source = self._store.read(slug)
            if source is None:
                raise UserToolError(f"no tool file for {slug!r}")
            cls = self.compile_tool(source, filename=str(self._store.path_for(slug)))
            self._register(source.name, cls)
        except (
            Exception
        ) as exc:  # one broken file must never break startup or its siblings
            self._log.exception("smart_notes: skipped the user tool %r", slug)
            return UserToolLoad(slug, error=_reason(exc))
        return UserToolLoad(slug)

    def compile_tool(self, source: UserToolSource, *, filename: str = "") -> type[Tool]:
        """Check, execute and return ``source``'s tool class WITHOUT registering it globally.

        Used for both a real load and the dialog's test run, so a candidate is compiled by
        exactly the code that will later load it. The registry is restored before returning
        either way: registration is the caller's decision (:meth:`load` makes it, the test box
        does not).

        Args:
            source: The tool's slug + code.
            filename: The path to attribute the code to in tracebacks (defaults to a marker
                naming the draft).

        Returns:
            The ``Tool`` subclass the module registered.

        Raises:
            UserToolError: When the guard refuses it, the module raises while executing, or it
                did not register exactly ``user:<slug>``.
        """
        validate_slug(source.slug)
        self._guard.check(source.code)
        name = source.name
        module = ModuleType(_MODULE_PREFIX + source.slug.replace("-", "_"))
        module.__file__ = filename or f"<omnia user tool {source.slug}>"
        before = dict(TOOL_REGISTRY)
        TOOL_REGISTRY.pop(name, None)  # a reload must not read as a name conflict
        baseline = set(TOOL_REGISTRY)
        sys.modules[module.__name__] = module
        try:
            exec(compile(source.code, module.__file__, "exec"), module.__dict__)
            added = set(TOOL_REGISTRY) - baseline
            if added != {name}:
                raise UserToolError(
                    f"a user tool must register exactly {name!r}; this one registered "
                    + (", ".join(repr(other) for other in sorted(added)) or "nothing")
                )
            cls = TOOL_REGISTRY[name]
            if not (isinstance(cls, type) and issubclass(cls, Tool)):
                raise UserToolError(
                    f"{name!r} is not a Tool subclass, so nothing could run it"
                )
            return cls
        except UserToolError:
            raise
        except Exception as exc:
            raise UserToolError(f"the tool failed to load: {_reason(exc)}") from exc
        finally:
            TOOL_REGISTRY.clear()
            TOOL_REGISTRY.update(before)

    def unload_all(self) -> None:
        """Unregister every ``user:`` tool in the registry (the plugin's disable teardown).

        Deliberately keyed on the NAMESPACE rather than on this instance's bookkeeping. A
        loader owns ``user:`` as a whole — that is what lets it rebind an edited file — and more
        than one instance can exist in a process: the plugin builds one at enable, and the
        settings dialog builds another when it opens. Iterating ``self._loaded`` meant a tool
        authored in the dialog survived disabling the feature, because the plugin's loader had
        never heard of it. The registry is the shared truth, so teardown reads it.
        """
        for name in [n for n in registered_tools() if n.startswith(USER_TOOL_PREFIX)]:
            self._unregister(name)

    def _register(self, name: str, cls: type[Tool]) -> None:
        """Bind ``name`` to ``cls``, replacing a previous load of the same tool."""
        unregister_tool(name)  # only ever a `user:` name this loader owns
        register_tool(name)(cls)
        self._loaded.add(name)

    def _unregister(self, name: str) -> None:
        """Drop ``name`` from the registry and from this loader's bookkeeping."""
        unregister_tool(name)
        self._loaded.discard(name)


def _reason(exc: Exception) -> str:
    """A one-line, user-facing reason for a failure (its message, else its type)."""
    return str(exc) or exc.__class__.__name__


#: What a tool reaching for one of these modules is actually able to do, in the reader's terms.
#: The point is not to scare — it is that "this tool writes files and runs programs" belongs
#: ABOVE the code, not buried on line 40 of it, now that the import guard permits both.
_RISK_BY_MODULE: dict[str, str] = {
    "subprocess": "runs other programs on your computer",
    # `os.system` and `os.popen` run programs, so this must say so — the model is told
    # "pathlib, subprocess and the rest are available", which makes os.system(f"ffmpeg …") a
    # likely generation, and a reviewer who has learned that "runs other programs" is how that
    # reads would otherwise conclude this tool does not.
    "os": "reads and changes files and folders, and can run other programs",
    "shutil": "copies, moves and deletes files",
    "pathlib": "reads and writes files",
    "io": "reads and writes files",
    "tempfile": "creates temporary files",
    "wave": "reads and writes audio files",
    "omnia.core.audio": "decodes and re-encodes audio (runs the audio runtime)",
}

#: What a bare ``open()`` means, kept beside the module map because it is the SAME sentence.
#: ``open`` is a builtin — no import to walk — and it is precisely the call this guard stopped
#: refusing, on the grounds that the review would be told instead. A walk that looked only at
#: imports missed it, so a tool doing `open(dir + "/" + name, "wb")` with no imports at all
#: raised no banner, and an absent banner affirmatively says "this only reshapes text".
_OPEN_RISK = "reads and writes files"


def risky_operations(code: str) -> list[str]:
    """Return plain-language descriptions of what ``code`` reaches for, for the review screen.

    The import allowlist stopped being the safety boundary the moment it had to permit ``os``
    and ``subprocess`` (see :class:`ImportGuard`); the mandatory read-and-run review is. A
    review is only worth something if the reader knows what to look for, and "spot the
    subprocess import in forty lines of generated Python" is not a fair ask — especially when
    the Python was written by a model and is being read once.

    Deliberately import-based rather than clever: a tool that hides its intent defeats a
    heuristic anyway, and the case that matters here is the honest tool whose reach the reader
    simply did not notice.

    Known limit worth stating: a tool using ``ctx.audio`` — which the authoring prompt actively
    recommends — needs no import, so nothing is reported for it. That is the intended reading
    (the audio runtime is Omnia's own managed process, not the tool reaching out), but it does
    mean the ``omnia.core.audio`` entry only fires on a direct import of the module.

    Args:
        code: The module source.

    Returns:
        Unique descriptions in a stable order; empty when the tool only transforms text.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []  # the guard reports this properly; nothing to describe
    found: list[str] = []

    def note(risk: str) -> None:
        if risk and risk not in found:
            found.append(risk)

    def note_module(name: str) -> None:
        """Match a dotted import against the map by longest prefix.

        Both directions matter: ``import os`` is the bare key, while
        ``from omnia.core.audio.sidecar import ...`` has to find the ``omnia.core.audio``
        entry. Keying only on the first segment would file every ``omnia.*`` import under
        ``omnia``; keying only on the full name misses the submodule.
        """
        parts = (name or "").split(".")
        for size in range(len(parts), 0, -1):
            risk = _RISK_BY_MODULE.get(".".join(parts[:size]))
            if risk:
                note(risk)
                return

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                note_module(alias.name)
        elif isinstance(node, ast.ImportFrom):
            note_module(node.module or "")
        # The builtin needs no import, so the import walk above cannot see it — and it is the
        # one call this guard stopped refusing.
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "open"
        ):
            note(_OPEN_RISK)
    return found


@dataclass(frozen=True)
class ToolInput:
    """One control the Try-it form renders: the field it stands for, and what that field holds.

    ``kind`` is one of :data:`~omnia.plugins.smart_notes.engine.tools.base.INPUT_KINDS`, which
    is what decides whether the control is a box to type in or a button that opens a file
    browser filtered to that family.
    """

    field: str
    kind: str


def declared_inputs(code: str) -> list[ToolInput]:
    """Return the inputs ``code`` declares, read from its source WITHOUT executing it.

    This is the sibling of :func:`risky_operations`: source text in, metadata out, one
    ``ast.parse``, no ``exec``. That is the whole point. Compiling the draft — the only other
    way to reach ``Tool.input_kinds`` — runs the module, and in this feature arbitrary
    execution happens exactly once, on Run, AFTER the risk banner has told the user what the
    code reaches for. Building the test form needs the inputs BEFORE Run, so discovering them
    by compiling would move execution ahead of the review that exists to precede it, which is
    the one safety property the whole authoring flow is built around.

    The price is stated plainly: a tool that COMPUTES its ``input_kinds`` (a comprehension, a
    dict built at class-creation time) is not readable this way, and falls back to the single
    text box below. Its field then arrives empty, the tool declines, and the user sees that in
    the result — a visible decline rather than a silently wrong value, which is the right
    direction to fail.

    Args:
        code: The module source as it stands in the editor.

    Returns:
        One :class:`ToolInput` per declared field, in the order the literal lists them. NEVER
        empty: a draft with no ``input_kinds``, an empty one, a computed one or a syntax error
        all yield the single :data:`SAMPLE_FIELD` text input. That row is not a dead end — a
        text row carries its own attach button, so a tool that declares nothing (every tool
        authored before this reader existed) can still be handed a file; what it loses is the
        field's real NAME and the picker's filter, not the ability to be tested.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return [ToolInput(SAMPLE_FIELD, TEXT_INPUT)]
    inputs: list[ToolInput] = []
    for name, kind in _input_kinds_literal(tree):
        field_name = name.strip()
        if not field_name:
            continue
        # An unrecognised value is a typo in generated code, and a typed box — which can still
        # take a file — is the harmless reading of it.
        inputs.append(
            ToolInput(field_name, kind if kind in INPUT_KINDS else TEXT_INPUT)
        )
    return inputs or [ToolInput(SAMPLE_FIELD, TEXT_INPUT)]


def _input_kinds_literal(tree: ast.AST) -> list[tuple[str, str]]:
    """Return the ``input_kinds`` a CLASS BODY in ``tree`` declares (``[]`` when none does).

    Only a class body counts. ``input_kinds`` is a ``Tool`` ClassVar, and ``ast.walk`` is
    breadth-first: a module-level name or a params-model field that happens to be spelled the
    same would otherwise be reached FIRST and shadow the real declaration — silently costing a
    media tool its file pickers. Which class is not checked beyond that, because a user-tool
    module defines exactly one ``Tool`` subclass by contract and ``compile_tool`` enforces it.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for statement in node.body:
            # Both forms: `input_kinds = {...}` and the annotated `input_kinds: ClassVar[...] =
            # {...}` the authoring prompt's ClassVar style produces. An annotation with no value
            # is a declaration, so `value` is None and the reader falls back.
            if isinstance(statement, ast.AnnAssign):
                targets: list[ast.expr] = [statement.target]
                value: Optional[ast.expr] = statement.value
            elif isinstance(statement, ast.Assign):
                targets = list(statement.targets)
                value = statement.value
            else:
                continue
            if any(
                isinstance(target, ast.Name) and target.id == "input_kinds"
                for target in targets
            ):
                return _string_pairs(value)
    return []


def _string_pairs(node: Optional[ast.expr]) -> list[tuple[str, str]]:
    """Return ``node``'s items when it is a dict of string constants, else ``[]``.

    All-or-nothing on purpose: a mapping with one computed entry is one this reader cannot
    describe, and half a form is worse than the honest single-box fallback.
    """
    if not isinstance(node, ast.Dict):
        return []
    pairs: list[tuple[str, str]] = []
    # strict: ast guarantees the two lists are the same length, and a `**other` entry shows up
    # as a None key — which the string check below rejects, taking the whole literal with it.
    for key, value in zip(node.keys, node.values, strict=True):
        if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
            return []
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            return []
        pairs.append((key.value, value.value))
    return pairs


class ReviewGate:
    """Remembers which exact sources the user has actually TEST-RUN, so Save can require one.

    The gate is server-side on purpose. "Disable the Save button" is a UI courtesy; the rule
    that generated code is never persisted unseen has to hold wherever the op is called from,
    so the controller asks this object and refuses. Keyed by a digest of the source text, which
    makes editing the code after a test re-arm the gate — the user tests what they save.

    Session-scoped (it lives on the dialog controller): closing the dialog forgets everything,
    which is the safe direction.
    """

    def __init__(self) -> None:
        self._tested: set[str] = set()

    def mark_tested(self, code: str) -> None:
        """Record that ``code`` was executed once with the user watching."""
        self._tested.add(source_fingerprint(code))

    def is_tested(self, code: str) -> bool:
        """Return whether exactly this source has been test-run in this session."""
        return source_fingerprint(code) in self._tested


@dataclass(frozen=True)
class ToolTestResult:
    """What one test run of a candidate tool produced, ready for the dialog to render.

    ``kind``/``data``/``ext`` carry a produced MEDIA result out of the worker thread. They are
    additive and default to "nothing", so ``output`` stays exactly what it was — the honest
    one-line summary a text box can show — and a caller that only reads ``output`` keeps
    working. Without them the bytes died in the worker's closure and the dialog could only ever
    print "(tts: 55296 bytes of .mp3)", which is not something a person can listen to.
    """

    status: str  # produced | not_applicable | empty | error
    output: str = ""
    detail: str = ""
    kind: str = ""
    data: Optional[bytes] = None
    ext: str = ""

    @property
    def ok(self) -> bool:
        """Whether the tool produced a result (a decline is a valid, non-ok outcome)."""
        return self.status == "produced"


class UserToolTester:
    """Runs a candidate tool once against the values the Try-it form collected.

    Deliberately NOT the pipeline: the pipeline resolves tools by name from the global registry,
    and a candidate that has not been saved must not be registered anywhere. This calls the same
    two methods the pipeline calls (:meth:`Tool.parse_params` then :meth:`Tool.run`) against a
    note built from the form, and maps the outcome the same way.
    """

    #: Kept as a class attribute so existing callers keep reading it here; the module constant
    #: is the definition, because the AST reader that builds the form has to agree with it.
    SAMPLE_FIELD = SAMPLE_FIELD

    def run(
        self,
        cls: type[Tool],
        *,
        inputs: Mapping[str, str],
        params: Mapping[str, Any],
        ctx: ToolContext,
    ) -> ToolTestResult:
        """Run ``cls`` once against ``inputs`` and describe what came back.

        Args:
            cls: The compiled candidate tool class.
            inputs: ``{field name: value}`` from the Try-it form — one entry per input the tool
                declares, so a tool reading two fields sees both. An empty map still runs, under
                the single :data:`SAMPLE_FIELD`, which is what keeps a stale page and an
                input-less tool testable.
            params: The params the user set in the test form.
            ctx: The live tool context (a tool may legitimately read it; a deterministic one
                should not).

        Returns:
            The outcome as :class:`ToolTestResult` — including ``status="error"`` when the tool
            raised, because seeing the failure IS a test run.

        Raises:
            UserToolError: When the tool declares no generation kind this build implements.
        """
        fields = dict(inputs) or {SAMPLE_FIELD: ""}
        rule = self._rule(cls, fields)
        try:
            request = ToolRequest(
                rule=rule,
                fields=fields,
                params=cls.parse_params(params),
            )
            outcome = cls().run(request, ctx)
        except Exception as exc:
            return ToolTestResult("error", detail=_reason(exc))
        return self._explain_unoffered(cls, params, fields, self._describe(outcome))

    def _explain_unoffered(
        self,
        cls: type[Tool],
        params: Mapping[str, Any],
        fields: Mapping[str, str],
        result: ToolTestResult,
    ) -> ToolTestResult:
        """Name the fields the tool READS that the form never offered a control for.

        The form is built from ``input_kinds`` and the tool reads whatever its params point at;
        nothing makes the two agree. When they do not — the declaration says ``"Audio"`` and the
        param defaults to ``"Clip"`` — the panel shows a plausible, correctly-filtered row, the
        user picks a real file for it, and the tool reads an empty string and declines. Every
        visible signal says it worked; the result is "it produced nothing" with no cause given.

        Only for a run that produced nothing: a tool that worked has nothing to explain.

        Args:
            cls: The compiled candidate.
            params: The params the run used (unvalidated — this is the same map ``run`` got).
            fields: The note the form built, i.e. exactly the controls the user could fill in.
            result: What the run produced.

        Returns:
            ``result``, with the mismatch appended to its detail when there is one.
        """
        if result.ok:
            return result
        try:
            referenced = cls.referenced_fields(cls.parse_params(params))
        # Broad: `referenced_fields` is user code on a path that is only ADDING an explanation,
        # so a tool that cannot answer must cost the explanation and nothing else.
        except Exception:
            return result
        offered = {name.strip().lower() for name in fields}
        missing = [
            name for name in referenced if name and name.strip().lower() not in offered
        ]
        if not missing:
            return result
        note = (
            f"It reads {', '.join(missing)}, which this form did not offer a box for: the "
            "fields its `input_kinds` declares and the fields its params point at do not "
            "match. Make one agree with the other and run it again."
        )
        detail = f"{result.detail} {note}" if result.detail else note
        return replace(result, detail=detail)

    def _rule(self, cls: type[Tool], fields: Mapping[str, str]) -> SmartNotesFieldRule:
        """Build the one-rule note a test runs against, named after the FIRST input.

        The first input is what a tool with a blank ``<something>_field`` param resolves to:
        ``rule_source_fields`` → ``_first_value`` reads ``sources[0]``, which is the rule's only
        prompt ref. With nothing declared that is ``"Sample"``, so the rule is character-
        identical to the one this tester has always built; with a declaration it is the tool's
        own default field, so both lookup paths land on a value the user actually typed.

        Raises:
            UserToolError: When the tool declares no generation kind this build implements —
                the rule could not be built at all, so the tool could never run.
        """
        kinds = sorted(getattr(cls, "kinds", frozenset()))
        kind = next((k for k in kinds if k in GENERATION_KINDS), "")
        if not kind:
            raise UserToolError(
                "the tool declares no generation kind it can serve — `kinds` must contain "
                + ", ".join(GENERATION_KINDS)
            )
        first = next(iter(fields))
        return SmartNotesFieldRule(
            kind=kind,
            target_field="Preview",
            base_field=first,
            source_field=first,
            prompt=f"{{{{{first}}}}}",
        )

    @staticmethod
    def _describe(outcome: object) -> ToolTestResult:
        """Render a tool outcome as the test box's result."""
        if isinstance(outcome, Produced):
            result = outcome.result
            if result.text:
                return ToolTestResult("produced", output=result.text)
            size = len(result.data or b"")
            # `output` stays the honest one-line summary — it is what a caller with only a text
            # box shows. The bytes ride ALONGSIDE it so the dialog can render the file by what
            # it actually is, rather than describing it.
            return ToolTestResult(
                "produced",
                output=f"({result.kind}: {size} bytes of .{result.ext or '?'})",
                kind=result.kind,
                data=result.data,
                ext=result.ext,
            )
        if isinstance(outcome, NotApplicable):
            return ToolTestResult("not_applicable", detail=outcome.reason)
        if isinstance(outcome, Empty):
            return ToolTestResult("empty", detail=outcome.reason)
        return ToolTestResult(
            "error", detail=f"returned {outcome!r}, not a tool outcome"
        )
