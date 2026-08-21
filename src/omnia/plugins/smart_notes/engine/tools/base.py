"""The Tool contract: what a generation tool is handed, and what it may hand back.

A *tool* is one way to fill one generated field. A field carries an ORDERED chain of them and
the :class:`~omnia.plugins.smart_notes.engine.tools.pipeline.GenerationPipeline` runs the chain
until one produces a result — so a deterministic tool can decline (no LLM spend) and let the
provider-backed ``"ai"`` tool take over. This module holds only the contract; the concrete
tools live beside it and self-register through
:func:`~omnia.plugins.smart_notes.engine.tools.registry.register_tool`.

The outcome taxonomy is the heart of the fallback semantics:

* :class:`Produced` — the field's content; the chain stops here.
* :class:`NotApplicable` — a precondition was unmet, so the tool never ran its transform. Falls
  through silently; this is what the whole feature exists for.
* :class:`Empty` — the tool ran and got nothing meaningful. Falls through too, but is
  distinguishable in the trace from "couldn't even try".
* *breakage is NOT a return value* — a tool RAISES (:class:`ToolError`, or anything else). The
  pipeline records the attempt as an error and still falls through, which keeps the
  raise-on-failure contract the generators already have
  (:meth:`~omnia.plugins.smart_notes.engine.generators.Generator.generate`).

Pure logic — no ``aqt``/``anki`` imports, so tools unit-test headless.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Final, Optional

from omnia.core.audio.sidecar import AudioSidecar
from omnia.core.providers.errors import ProviderError

if TYPE_CHECKING:
    import logging

    from pydantic import BaseModel

    from omnia.core.providers import ProviderHub
    from omnia.plugins.smart_notes.config import SmartNotesFieldRule
    from omnia.plugins.smart_notes.engine.generators import GenerationResult
    from omnia.plugins.smart_notes.engine.language import LanguageDetector


#: What testing one of a tool's inputs asks of a person. ``"text"`` is typed in place; every
#: other value means "pick a file", and names the family the picker filters to. Kept separate
#: from the OUTPUT vocabulary (``GENERATION_KINDS``) on purpose: a tool that reads a video and
#: writes text has an input kind and an output kind that must not be confused, and video is a
#: legitimate input while nothing in this add-on generates one.
INPUT_KINDS: Final[tuple[str, ...]] = ("text", "image", "audio", "video", "file")

#: The default for any input a tool does not describe.
TEXT_INPUT: Final = "text"

#: The ONE per-kind extension vocabulary: which files belong to which media family.
#:
#: It answers two questions that must never disagree — what the file picker offers for an input
#: of this kind, and which family a file already in hand belongs to (``media_sample`` derives
#: its ``<img>``-vs-``[sound:]`` split and its produced-file classification from it). They were
#: written out separately once, and the copies drifted: the picker hid ``.bmp``/``.tiff`` scans
#: that the rest of the code happily called pictures. Every consumer reads this table.
#:
#: ``"file"`` deliberately has no extensions — it is the escape hatch for a tool reading
#: something this table does not anticipate, and a filter that silently hides the right file is
#: worse than no filter at all.
INPUT_KIND_EXTENSIONS: Final[Mapping[str, tuple[str, ...]]] = {
    "image": (
        "png",
        "jpg",
        "jpeg",
        "gif",
        "webp",
        "svg",
        "avif",
        "bmp",
        "tif",
        "tiff",
    ),
    "audio": ("mp3", "wav", "ogg", "m4a", "flac", "opus"),
    "video": ("mp4", "mkv", "webm", "mov", "avi"),
    "file": (),
}


class ToolError(ProviderError):
    """Raised by a tool that BROKE (bad params, provider failure, unusable input).

    A :class:`~omnia.core.providers.errors.ProviderError` subclass, mirroring
    :class:`~omnia.plugins.smart_notes.engine.ordering.SmartNotesCycleError`, so every caller
    that already handles provider failures handles a tool failure too. Raising is the ONLY way
    a tool reports breakage — the three outcome types all mean "no result, and that's fine".
    """


def resolve_media_dir() -> str:
    """The collection's media folder, for a :class:`ToolContext` that should have the real one.

    Lives here rather than in ``engine/service.py`` because it is not service-specific: it is
    how ANY tool context resolves media, and there are two constructors. Having it next to the
    default it replaces is what stops one of them being wired and the other forgotten — which
    is exactly what happened, leaving the dialog's Test run with no media folder while
    generation had one.

    ``anki_compat`` is imported INSIDE the call, so this module stays headless and the
    collection is touched only when a tool actually asks — on the worker thread, after Anki
    exists.
    """
    from omnia.core import anki_compat

    try:
        return anki_compat.media_dir()
    # Broad on purpose: a tool asking where the media lives must get "" and decline cleanly,
    # not take the field down because the collection was closed mid-run.
    except Exception:  # pragma: no cover - defensive
        return ""


def _no_media_dir() -> str:
    """The default :attr:`ToolContext.media_dir`: no collection is reachable.

    Returns "" rather than raising, because the caller a tool makes is
    ``ctx.media_dir()`` and an empty string is a value it can test — a tool that needs media
    can decline cleanly instead of dying with an Anki traceback in a headless build or a test.
    """
    return ""


@dataclass(frozen=True)
class ToolContext:
    """Everything a tool may touch. Built once per :class:`GenerationService`.

    Deliberately narrow (ISP): a tool gets the provider hub, the best-effort language detector
    the TTS path needs, the audio codec runtime, and a logger — no collection, no config store,
    no Anki.

    ``audio`` is here rather than constructed inside a tool for the same reason ``providers`` is
    (DIP): the managed-venv sidecar it drives resolves the PROCESS-WIDE runtime manager, so a
    tool that built its own could only be tested by patching a module global. It defaults to a
    real one, which costs nothing — the manager is resolved lazily, on first use.
    """

    providers: ProviderHub
    detector: LanguageDetector
    logger: logging.Logger
    audio: AudioSidecar = field(default_factory=AudioSidecar)
    #: The collection's media folder, or "" when there is no collection (tests, a headless
    #: build). A tool that converts a file needs it because a note stores only the bare
    #: filename; deriving it per-tool would mean guessing a per-platform profile path.
    #: Callable, not a string, so building a context never touches Anki — the folder is
    #: resolved on first use, inside the tool, on the worker thread.
    media_dir: Callable[[], str] = _no_media_dir


@dataclass(frozen=True)
class ToolRequest:
    """One tool invocation: the compiled rule, the note's fields, and this tool's params.

    ``fields`` is the working map :meth:`GenerationService.generate_note` maintains, so a tool
    reads freshly chained values exactly as the generators do — a READ-ONLY view of it while a
    level is in flight, so a tool that tries to mutate its inputs fails loudly instead of
    silently changing what a sibling field sees. ``params`` are the field's per-tool params
    AFTER :meth:`Tool.parse_params` validated them (defaults filled in). ``note_id`` is the
    note being generated, so a tool's diagnostics can name it: with several notes in flight,
    position in the log no longer identifies the note.
    """

    rule: SmartNotesFieldRule
    fields: Mapping[str, str]
    params: Mapping[str, Any] = field(default_factory=dict)
    note_id: int = 0


@dataclass(frozen=True)
class Produced:
    """The tool generated the field's content — the chain stops here."""

    result: GenerationResult


@dataclass(frozen=True)
class NotApplicable:
    """A precondition was unmet, so the tool declined to run (never a failure).

    ``reason`` is the human-readable precondition ("word 'run' not found in Sentence"); it is
    recorded in the pipeline trace and shown in diagnostics.
    """

    reason: str = ""


@dataclass(frozen=True)
class Empty:
    """The tool ran its transform and got nothing meaningful back (dictionary miss, no match).

    Kept distinct from :class:`NotApplicable` — "I ran and found nothing" is a different thing
    to tell the user than "I never applied here", and the two already read differently in the
    chain summary
    (:func:`~omnia.plugins.smart_notes.engine.tools.pipeline.summarize_attempts`), which the
    user sees when an exhausted chain surfaces as a ``ToolChainError`` in the preview, the
    prompt palette or the account dialog. At the NOTE level both are the same verdict (a
    ``FailedField`` of kind ``"unproductive"``): nothing is wrong, there was just nothing to
    make — the batch summary renders only a count, not this text.
    """

    reason: str = ""


ToolOutcome = Produced | NotApplicable | Empty


class Tool(ABC):
    """One way to fill one generated field.

    Subclasses are **stateless** and must be constructible with no arguments — the registry
    instantiates them on resolve and hands everything they need through :meth:`run`'s
    ``request``/``ctx`` (DIP), so the same instance is safe on any worker thread.

    **``run`` may execute concurrently with itself.** Since bounded concurrency landed, a level's
    fields — and several notes of a batch — are dispatched together, so the same tool CLASS runs
    on several threads at once for different notes. The engine guarantees the tool's INPUTS are
    safe (a frozen read-only field map per level, a fresh instance per resolve); it can guarantee
    nothing about a tool's SIDE EFFECTS. A tool that writes a fixed scratch path, or names its
    output after the field rather than the note, will put one note's output in another note's
    field with no error anywhere. Derive every path from ``request.note_id`` or a ``tempfile``,
    and keep no mutable state on the class.

    Class attributes:
        name: The stable config key the field's chain stores (``"ai"``, ``"cloze"``, …).
        label: Short human name for the tools picker.
        description: One line explaining what the tool does, shown in the picker.
        kinds: The generation kinds it can serve (subset of ``{"text", "image", "tts"}``). The
            pipeline skips a tool whose ``kinds`` do not cover the rule's kind.
        deterministic: True when the tool never calls a paid/LLM endpoint.
        uses_provider: True when the tool generates through the row's configured
            Provider/Model/Voice. Deliberately NOT the inverse of ``deterministic``: those two
            answer different questions and ``cloze_audio`` is the proof — it is deterministic
            (it never invents text, so it costs no LLM tokens) yet it synthesizes speech with
            the row's voice, so those cells very much apply to it. The settings row fades
            Provider/Model/Voice when NO tool in the chain uses them, which is only sound with
            the property that actually means it.
        required_params: Param names the tools picker refuses to leave blank. A param whose
            blank default silently resolves to something else (``cloze``'s ``sentence_field``
            falls back to the rule's first prompt ref) is a footgun in a picker: the user sees
            an empty box and cannot tell which field the tool will actually read. Naming them
            here lets the picker reject Done — with the tool and param named — instead of the
            mistake surfacing later as a wrong or silently-skipped generation. The RUNTIME
            keeps honouring the fallbacks: a chain synced from a device on an older Omnia
            predates this validation and must still generate.
        params_model: Pydantic model validating the field's per-tool params (None = no params).
        input_kinds: ``{field name: one of INPUT_KINDS}`` — what each field the tool READS
            holds. ``referenced_fields`` already says WHICH fields a tool reads; this says what
            is in them, which is what lets the Try-it form offer a file browser for a clip
            instead of asking someone to type ``[sound:x.mp3]`` by hand. It is a DECLARATION,
            read from the module's source text without executing it (see
            :func:`~omnia.plugins.smart_notes.engine.tools.user_tools.declared_inputs`), so it
            must be a literal dict of literal strings; anything else, and anything left out,
            falls back to a text box that can still take an attached file.
    """

    name: ClassVar[str]
    label: ClassVar[str]
    description: ClassVar[str]
    kinds: ClassVar[frozenset[str]]
    deterministic: ClassVar[bool]
    uses_provider: ClassVar[bool] = True
    required_params: ClassVar[frozenset[str]] = frozenset()
    params_model: ClassVar[Optional[type[BaseModel]]] = None
    input_kinds: ClassVar[Mapping[str, str]] = {}

    @abstractmethod
    def run(self, request: ToolRequest, ctx: ToolContext) -> ToolOutcome:
        """Try to generate ``request``'s field.

        Args:
            request: The rule, the note's working field map, and the validated params.
            ctx: The providers/detector/logger the tool may use.

        Returns:
            :class:`Produced` with the content, or :class:`NotApplicable` /:class:`Empty` to
            let the next tool in the chain try.

        Raises:
            ToolError: When the tool BROKE. The pipeline records it and falls through, so a
                later tool can still fill the field.
        """

    @classmethod
    def parse_params(cls, params: Mapping[str, Any]) -> dict[str, Any]:
        """Validate ``params`` against :attr:`params_model`, filling in its defaults.

        Called by the pipeline immediately before :meth:`run`, INSIDE the attempt's try-block:
        a params model that rejects the stored dict turns that tool into an error attempt and
        the chain continues, rather than failing the whole field.

        Args:
            params: The field's raw per-tool params, as stored in config.

        Returns:
            The validated params (the raw mapping as a dict when the tool declares no model).

        Raises:
            pydantic.ValidationError: If ``params`` does not satisfy :attr:`params_model`.
        """
        if cls.params_model is None:
            return dict(params)
        validated: dict[str, Any] = cls.params_model(**params).dict()
        return validated

    @classmethod
    def reads_prompt(cls, params: Mapping[str, Any]) -> bool:
        """Whether this tool, configured with ``params``, READS the field's prompt.

        What it decides is whether the prompt's ``{{refs}}`` are real dependency edges
        (:func:`~omnia.plugins.smart_notes.engine.rules.rule_source_fields`). False means "my
        inputs are the fields my params name, and nothing else", so a prompt left behind from an
        earlier configuration is dead text rather than a set of edges the tool will never honour
        — and a stale HARD edge onto a field that is empty on most notes blocks generation
        forever with nothing to show why.

        It takes ``params`` because for the tools that answer False the honest answer depends on
        them: ``cloze``'s ``sentence_field`` and ``cloze_audio``'s ``source_field`` fall back to
        the rule's first prompt ref when left blank (chains synced from a release before those
        params were required), and a chain that reads the prompt through that fallback must keep
        the edges the fallback depends on.

        Not derivable from :attr:`deterministic` or :attr:`uses_provider`, which answer different
        questions: ``cloze_audio`` is deterministic AND provider-using AND does not read the
        prompt. Three flags because there are three questions.

        Args:
            params: The tool's validated params for this field.

        Returns:
            True by default, so a tool that never considered the question keeps every edge it
            would have had.
        """
        return True

    @classmethod
    def referenced_fields(cls, params: Mapping[str, Any]) -> list[str]:
        """Return the note fields ``params`` names (e.g. a ``sentence_field`` param).

        These join the rule's derived prerequisites in
        :func:`~omnia.plugins.smart_notes.engine.rules.rule_prerequisites`, so a tool that
        reads another field orders and blocks through the SAME single source of truth the
        prompt ``{{refs}}`` use — the dependency graph and the topological sort inherit tool
        edges with no extra plumbing.

        Implementations MUST NOT raise: this runs on the ordering/graph path with params that
        have not been through :meth:`parse_params`, so read them defensively.

        Args:
            params: The field's raw per-tool params.

        Returns:
            Field names in the order they should be considered (empty by default).
        """
        return []

    @classmethod
    def availability(cls, ctx: ToolContext) -> str | None:
        """Return what this machine is missing for the tool, or None when nothing is.

        Purely **advisory**, and the picker MUST render it without disabling the tool: an
        unavailable tool still runs when configured. A tool cannot see the row it will run on
        (``cloze_audio``'s answer depends on the VOICE the field resolves to, which is per-row),
        so a global verdict is a hint — "MP3 voices need the audio runtime" — never a gate. The
        two real gates are structural and the picker owns them: the tool is not installed here,
        or its :attr:`kinds` do not cover the row's kind.
        """
        return None
