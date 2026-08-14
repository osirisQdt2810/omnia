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
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, Optional

from omnia.core.logging import get_logger
from omnia.plugins.smart_notes.config import SmartNotesFieldRule
from omnia.plugins.smart_notes.engine.tools.base import (
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

_GENERATION_KINDS = ("text", "image", "tts")

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
    """Refuses a user-tool module that reaches outside a small allowlist of imports.

    A **speed bump, not a sandbox** (see the module docstring). It exists so the source the user
    reads is the source that runs: a tool that quietly imported ``urllib.request`` would look
    like a string transform in the dialog and behave like a network client at review time. The
    allowlist is everything a note-field transform legitimately needs, plus the Omnia modules
    the ``Tool`` contract itself requires.
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
            "pydantic",
            "omnia.core.config.base",
            "omnia.core.lang",
            "omnia.core.text",
            "omnia.plugins.smart_notes.config",
            # The whole ENGINE package: the Tool contract, the outcome types, the result
            # dataclass and the pure helpers a tool defaults its params from
            # (``rules.rule_source_fields``). Everything under it is pure logic by the repo's
            # coupling rule — no ``aqt``/``anki`` — which is exactly the line being drawn here.
            "omnia.plugins.smart_notes.engine",
        }
    )

    #: Builtins whose mere presence means the module is doing something a field transform never
    #: needs. Flagged by NAME only — trivially bypassed, and worth catching anyway because the
    #: realistic failure here is a model that ignored its instructions, not an attacker.
    FLAGGED_CALLS: frozenset[str] = frozenset(
        {
            "__import__",
            "breakpoint",
            "compile",
            "eval",
            "exec",
            "globals",
            "input",
            "open",
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
        """Unregister every tool THIS loader registered (the plugin's disable teardown)."""
        for name in tuple(self._loaded):
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
    """What one test run of a candidate tool produced, ready for the dialog to render."""

    status: str  # produced | not_applicable | empty | error
    output: str = ""
    detail: str = ""

    @property
    def ok(self) -> bool:
        """Whether the tool produced a result (a decline is a valid, non-ok outcome)."""
        return self.status == "produced"


class UserToolTester:
    """Runs a candidate tool once against a sample value — the dialog's mandatory test.

    Deliberately NOT the pipeline: the pipeline resolves tools by name from the global registry,
    and a candidate that has not been saved must not be registered anywhere. This calls the same
    two methods the pipeline calls (:meth:`Tool.parse_params` then :meth:`Tool.run`) against a
    one-field note, and maps the outcome the same way.
    """

    #: The field name the sample value is presented under. Also the rule's base field and its
    #: only prompt ref, so a tool whose ``source_field`` param is blank still finds the sample
    #: through the same defaulting the builtins use.
    SAMPLE_FIELD = "Sample"

    def run(
        self,
        cls: type[Tool],
        *,
        sample: str,
        params: Mapping[str, Any],
        ctx: ToolContext,
    ) -> ToolTestResult:
        """Run ``cls`` once on ``sample`` and describe what came back.

        Args:
            cls: The compiled candidate tool class.
            sample: The value to place in the sample field.
            params: The params the user set in the test form.
            ctx: The live tool context (a tool may legitimately read it; a deterministic one
                should not).

        Returns:
            The outcome as :class:`ToolTestResult` — including ``status="error"`` when the tool
            raised, because seeing the failure IS a test run.
        """
        rule = self._rule(cls)
        try:
            request = ToolRequest(
                rule=rule,
                fields={self.SAMPLE_FIELD: sample},
                params=cls.parse_params(params),
            )
            outcome = cls().run(request, ctx)
        except Exception as exc:
            return ToolTestResult("error", detail=_reason(exc))
        return self._describe(outcome)

    def _rule(self, cls: type[Tool]) -> SmartNotesFieldRule:
        """Build the one-field rule a test runs against.

        Raises:
            UserToolError: When the tool declares no generation kind this build implements —
                the rule could not be built at all, so the tool could never run.
        """
        kinds = sorted(getattr(cls, "kinds", frozenset()))
        kind = next((k for k in kinds if k in _GENERATION_KINDS), "")
        if not kind:
            raise UserToolError(
                "the tool declares no generation kind it can serve — `kinds` must contain "
                + ", ".join(_GENERATION_KINDS)
            )
        return SmartNotesFieldRule(
            kind=kind,
            target_field="Preview",
            base_field=self.SAMPLE_FIELD,
            source_field=self.SAMPLE_FIELD,
            prompt=f"{{{{{self.SAMPLE_FIELD}}}}}",
        )

    @staticmethod
    def _describe(outcome: object) -> ToolTestResult:
        """Render a tool outcome as the test box's result."""
        if isinstance(outcome, Produced):
            result = outcome.result
            if result.text:
                return ToolTestResult("produced", output=result.text)
            size = len(result.data or b"")
            return ToolTestResult(
                "produced",
                output=f"({result.kind}: {size} bytes of .{result.ext or '?'})",
            )
        if isinstance(outcome, NotApplicable):
            return ToolTestResult("not_applicable", detail=outcome.reason)
        if isinstance(outcome, Empty):
            return ToolTestResult("empty", detail=outcome.reason)
        return ToolTestResult(
            "error", detail=f"returned {outcome!r}, not a tool outcome"
        )
