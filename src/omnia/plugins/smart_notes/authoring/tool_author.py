"""Compile a plain-English description of a transform into a user-authored ``Tool`` class.

The one LLM call in the user-tools story, and it happens exactly ONCE per tool — at authoring
time, in the Tools tab. What it returns is a complete Python module the user then reads, runs,
and saves; after that the tool is deterministic Python on disk and generating a field with it
costs nothing. That is the whole point of the feature: a transform a model does not need to
think about should not pay a model to do it.

The system prompt is deliberately narrow. It fixes the file's shape (one ``Tool`` subclass, one
``@register_tool("user:<slug>")``), the outcome vocabulary, and the imports a tool may use —
the same allowlist :class:`~omnia.plugins.smart_notes.engine.tools.user_tools.ImportGuard`
enforces at load time, so a compliant generation is not rejected a second later and a
non-compliant one is caught before it can ever be saved.

Pure logic: the message builder and the reply parser are module functions;
:class:`ToolAuthor` is the thin object that wraps an injected ``LLMProvider`` (DIP). No
``aqt``/``anki`` imports.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from omnia import envs
from omnia.core.providers.errors import ProviderError
from omnia.plugins.smart_notes.engine.tools import GENERATION_KINDS
from omnia.plugins.smart_notes.engine.tools.base import INPUT_KINDS
from omnia.plugins.smart_notes.engine.tools.user_tools import (
    SAMPLE_FIELD,
    ImportGuard,
    user_tool_name,
)

if TYPE_CHECKING:
    from omnia.core.providers.llm.base import LLMProvider

# A ```python fenced block, if the model wrapped its answer in one.
_FENCE_RE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL)

#: The model's ONLY permitted non-code reply: the marker, then why the request cannot be built
#: within the tool contract. Without an escape hatch the output rules force a module out of it
#: whatever it was asked for — and a plausible module that does the wrong thing is far worse
#: than a refusal, because it reads as a working answer and fails silently on real notes.
CANNOT_MARKER = "CANNOT:"

# The worked example the system prompt carries. It is the user's own motivating case ("from the
# audio filename, extract the extension") written exactly as a generated tool must look, which
# is far more reliable than describing the shape in prose — and it doubles as the spec the
# reviewer of this file can check a real generation against.
_EXAMPLE = '''from __future__ import annotations

import re
from typing import ClassVar

from pydantic import Field

from omnia.core.config.base import PersistedModel
from omnia.plugins.smart_notes.engine.generators import GenerationResult
from omnia.plugins.smart_notes.engine.tools import (
    Empty,
    NotApplicable,
    Produced,
    Tool,
    ToolContext,
    ToolOutcome,
    ToolRequest,
    register_tool,
)


class ExtractExtParams(PersistedModel):
    """Options for the extract-ext tool."""

    source_field: str = Field(
        "", description="Field holding the filename. Blank = this field's first prompt reference."
    )


@register_tool("user:extract-ext")
class ExtractExtTool(Tool):
    """Takes the file extension out of an audio filename."""

    name: ClassVar[str] = "user:extract-ext"
    label: ClassVar[str] = "Extract extension"
    description: ClassVar[str] = "Output just the extension of the filename in a field."
    kinds: ClassVar[frozenset[str]] = frozenset({"text"})
    deterministic: ClassVar[bool] = True
    params_model: ClassVar[type[PersistedModel]] = ExtractExtParams
    # One entry per field this tool can read. The test form is built from this dict, so a field
    # left out of it gets no control of its own. `source_field` defaults to blank, meaning "this
    # rule's first prompt reference" — there is no field NAME to key by, so it is declared under
    # "Sample", the name the form gives that implicit input. A param with a real default field
    # name is keyed by that name instead. Either way the VALUE is what the field holds: this one
    # holds a filename, i.e. ordinary text. A field holding a media reference would say "audio" /
    # "image" / "video", and the form would offer a file browser filtered to it.
    input_kinds: ClassVar[dict[str, str]] = {"Sample": "text"}

    @classmethod
    def referenced_fields(cls, params):
        name = str(params.get("source_field", "") or "").strip()
        return [name] if name else []

    def run(self, request: ToolRequest, ctx: ToolContext) -> ToolOutcome:
        name = str(request.params.get("source_field", "") or "").strip()
        value = _field_value(request.fields, name) if name else _first_value(request)
        if not value.strip():
            return NotApplicable("the source field is empty")
        match = re.search(r"\\.([A-Za-z0-9]{1,5})(?:\\]|\\s|$)", value)
        if match is None:
            return Empty("no file extension in the value")
        return Produced(GenerationResult("text", text=match.group(1).lower()))


def _field_value(fields, name):
    """Read a note field case-insensitively (Anki field names are)."""
    for key, value in fields.items():
        if key.strip().lower() == name.strip().lower():
            return str(value)
    return ""


def _first_value(request):
    """Fall back to the field this rule's prompt references first, else the base field."""
    from omnia.plugins.smart_notes.engine.rules import rule_source_fields

    sources = rule_source_fields(request.rule)
    fallback = sources[0] if sources else request.rule.base_field
    return _field_value(request.fields, fallback)
'''


def user_tool_system_prompt() -> str:
    """Build the system prompt that teaches the model to write ONE Omnia tool module.

    Returns:
        The persona + hard rules + the worked example, with the import allowlist rendered from
        :class:`ImportGuard` so the instruction can never drift from the check.
    """
    allowed = ", ".join(sorted(ImportGuard.ALLOWED_MODULES))
    # Rendered from the engine's own tuple, like `allowed` is from the guard's set, so the
    # prompt cannot drift from the tokens GenerationResult actually accepts.
    kind_tokens = ", ".join(repr(kind) for kind in GENERATION_KINDS)
    return (
        "You are a senior Python engineer writing ONE plugin file for the Anki add-on Omnia. "
        "The file defines a deterministic 'tool' that fills a single note field by "
        "TRANSFORMING other fields of the same note. It never calls an AI model.\n\n"
        "Output rules (all mandatory):\n"
        "1. Output ONLY the Python module. No prose, no explanation, no code fences — UNLESS "
        "rule 0 applies.\n"
        f"0. If the request CANNOT be satisfied within these rules, reply with one line: "
        f"'{CANNOT_MARKER} <what it would need, in plain language>' and nothing else. Do not "
        "approximate. A tool that renames a filename when the user asked to convert the file "
        "is worse than no tool: it looks correct, saves clean, and quietly writes wrong values "
        "to real notes. Reach for this when the request needs the NETWORK, or a service or "
        "program the user has not installed. Reading and writing files is allowed — see "
        "rule 9.\n"
        "2. Define EXACTLY ONE Tool subclass, decorated @register_tool with the exact name "
        "you are given, and set the same string as its `name` ClassVar.\n"
        "3. Declare the ClassVars: name, label (2-3 words), description (one sentence), "
        "kinds, deterministic = True, params_model, and input_kinds. `kinds` must match what "
        "the tool PRODUCES — see rule 10. It is NOT always text.\n"
        "3b. INPUT KINDS — the test form is BUILT from this dict, so it is not optional. "
        "`input_kinds` maps each field name the tool READS to what that field holds: one of "
        f"{', '.join(repr(k) for k in INPUT_KINDS)}. Key each entry by the field's NAME — for a "
        "`<something>_field` param with a real default, that default. A param whose default is "
        "BLANK means 'this rule's first prompt reference' and has no name to key by: declare it "
        f"under {SAMPLE_FIELD!r}, the name the form gives that implicit input. Keep the blank "
        "default when the tool should follow whatever field the rule points at — do NOT invent a "
        "field name just to have a key, because a made-up default silently overrides that "
        "behaviour on every note. Declare an entry for EVERY field the tool can name, the ones "
        "holding ordinary text included ('text'); an undeclared field gets no control of its own "
        "in the form. For a field holding a media reference ([sound:...] or <img ...>) name the "
        "family instead: 'image', 'audio', 'video', or 'file' when it could be anything — that "
        "is what makes the form offer a file browser filtered to that family rather than asking "
        "someone to type a filename by hand. A tool that reads a clip and writes text looks "
        "like this:\n"
        '       clip_field: str = Field("Audio", description="Field holding the clip.")\n'
        "       ...\n"
        '       input_kinds: ClassVar[dict[str, str]] = {"Audio": "audio"}\n'
        "   and the same tool following the rule's own field instead looks like this:\n"
        '       clip_field: str = Field("", description="Blank = this rule\'s first reference.")\n'
        "       ...\n"
        f'       input_kinds: ClassVar[dict[str, str]] = {{"{SAMPLE_FIELD}": "audio"}}\n'
        "   It must be a LITERAL dict of literal strings written in the class body — it is read "
        "from your source WITHOUT running it, so a computed one cannot be read at all. Get this "
        "wrong and the tool is harder to test even though it works.\n"
        "4. params_model is a pydantic model deriving from omnia.core.config.base."
        "PersistedModel, with a default for EVERY option (the tool must work with no params "
        "configured). Give each option a Field(description=...) — the settings UI renders the "
        "form from that schema. Name any option that holds a NOTE FIELD NAME `<something>_field`"
        " and return those names from `referenced_fields` so the dependency graph sees them.\n"
        "5. `run(self, request, ctx)` returns Produced(GenerationResult(<kind>, ...)) on "
        "success — see rule 10 for which kind and which payload — "
        "NotApplicable(reason) when a precondition is unmet (an empty source field), "
        "or Empty(reason) when the transform ran and found nothing. Both non-produced outcomes "
        "let the next tool in the field's chain try, so prefer them over raising.\n"
        "6. NEVER call an LLM/TTS provider, open a network connection, or touch the Anki "
        "collection through anki/aqt. "
        f"You may ONLY import from: {allowed}.\n"
        "7. Prefer plain string work on text the note already holds — most transforms need "
        "nothing else, and a tool that touches no file is easier to trust and to review.\n"
        "8. Keep it short, readable and commented where the logic is not obvious — a human "
        "reads this file before it is allowed to run.\n"
        "9. FILES AND MEDIA, when the request needs them. These are building blocks, not a "
        "template — reach for only the ones the request actually calls for:\n"
        "   * ctx.media_dir() is the collection's media folder, where a field's reference "
        '([sound:name.ext], <img src="name.ext">, or a bare filename) resolves. It returns '
        '"" when no collection is open — decline with NotApplicable then. Read with pathlib, '
        "and decline if the file is not there.\n"
        "   * To OUTPUT a new file, do NOT write it into the collection yourself: return "
        "Produced(GenerationResult(kind, data=<bytes>, ext=<the extension you produced>)) and "
        "Omnia stores it and writes the right reference into the field.\n"
        "   * ctx.audio is the installed AUDIO runtime: decode(bytes) -> WAV bytes for "
        "anything FFmpeg reads (an audio file, or the audio track of a video), and "
        "encode(WAV bytes) -> MP3 bytes. Use it rather than shelling out to ffmpeg, because it "
        "reports a proper error when the runtime is not installed. It does audio ONLY — for "
        "any other kind of file use the standard library, and fall back to rule 0 if that is "
        "not enough.\n"
        "   Everything else is ordinary Python: pathlib, subprocess and the rest are available "
        "when a transform genuinely needs them.\n"
        f"10. KIND. The tool's `kinds` ClassVar and the first argument of GenerationResult must "
        f"be the SAME token, and it is decided by what the tool produces. The tokens are "
        f"{kind_tokens}:\n"
        "   * text  -> GenerationResult('text', text=<str>)         a text transform\n"
        "   * tts   -> GenerationResult('tts', data=<bytes>, ext=<ext>)   audio (or a video's "
        "audio) — this is the kind for any SOUND file\n"
        "   * image -> GenerationResult('image', data=<bytes>, ext=<ext>) a picture\n"
        "   Getting this wrong fails SILENTLY in the worst way: returning bytes under 'text' "
        "makes the test run look successful and then writes an EMPTY field on real notes, and "
        "declaring kinds={'text'} on a tool that produces audio makes the pipeline skip it as "
        "wrong_kind for every sound field it was written for.\n\n"
        "Requests vary widely — reshaping text, deriving one field from another, cleaning "
        "up markup, renaming, counting, reformatting a list, converting a file. There is no "
        "typical tool. The example below is ONE arbitrary instance, included to show the "
        "required SHAPE: the imports, the ClassVars, the params model, the outcome types. Do "
        "not let its subject matter steer you — a tool that pattern-matches the example "
        "instead of solving the request is the single most common way this goes wrong.\n\n"
        f"{_EXAMPLE}"
    )


def build_user_tool_message(
    slug: str, description: str, field_names: list[str] | None = None
) -> str:
    """Build the user message asking for one tool, from the user's own description.

    Args:
        slug: The tool's slug; the module must register ``user:<slug>``.
        description: What the user typed ("from the audio filename, extract the extension").
        field_names: The note type's field names, so the model can name a sensible default
            source field (optional — a tool must work on any note type).

    Returns:
        The user message for the LLM call.
    """
    fields = ", ".join(name for name in (field_names or []) if name)
    context = (
        "THE DESCRIPTION WINS. If it names the fields the tool reads — say two sound fields "
        "called 'Audio 1' and 'Audio 2' — use those names verbatim as the param defaults and "
        "as the `input_kinds` keys, EVEN IF the note type below calls them something else. "
        "Someone describing the tool is telling you what its inputs should be; a note type's "
        "current field names are only a hint for when they did not say.\n"
        f"For context, one note type using this tool has the fields: {fields}. "
        "Fall back to the most fitting of THOSE as each `<something>_field` default ONLY when "
        "the description names no field, because a param defaulting to nothing is an input the "
        "test form cannot offer a control for. Read fields through the params either way — "
        "never by name in `run`.\n\n"
        if fields
        else ""
    )
    return (
        f'Write the tool. Register it as "{user_tool_name(slug)}".\n\n'
        f"What it must do:\n{description.strip()}\n\n"
        f"{context}"
        "Output only the Python module."
    )


def parse_user_tool_reply(raw: str, slug: str) -> str:
    """Extract the Python module from the model's reply and sanity-check it.

    Args:
        raw: The model's raw text reply.
        slug: The slug the module must register.

    Returns:
        The module source, fence-free and newline-terminated.

    Raises:
        ProviderError: When the reply is empty or does not register the expected tool — a
            failure worth showing the user, since the dialog cannot review what it did not get.
    """
    text = (raw or "").strip()
    if text.startswith(CANNOT_MARKER):
        reason = (
            text[len(CANNOT_MARKER) :].strip() or "it needs something a tool cannot do"
        )
        raise ProviderError(
            f"This cannot be built as a tool: {reason} A tool transforms text the note "
            "already holds — it cannot read or write files, run programs, use the network, or "
            "touch the collection."
        )
    match = _FENCE_RE.search(text)
    code = (match.group(1) if match else text).strip()
    if not code:
        raise ProviderError("the model returned no code")
    if user_tool_name(slug) not in code:
        raise ProviderError(
            f"the model did not register the tool as {user_tool_name(slug)!r} — try "
            "rephrasing the description, or edit the source before saving"
        )
    return code + "\n"


class ToolAuthor:
    """Turns a description into a tool module via an injected ``LLMProvider`` (DIP).

    One method, one call. Kept a class (not a function) for the same reason
    :class:`~omnia.plugins.smart_notes.authoring.author.PromptAuthor` is: the provider is a
    collaborator injected once and the dialog holds the object across a Generate and a
    Regenerate.
    """

    def __init__(self, llm: LLMProvider) -> None:
        self._llm = llm

    def generate(
        self, slug: str, description: str, field_names: list[str] | None = None
    ) -> str:
        """Write the tool module for ``description``.

        Args:
            slug: The tool's slug (the module registers ``user:<slug>``).
            description: The user's plain-English description of the transform.
            field_names: The current note type's fields, as context for defaults.

        Returns:
            The generated Python source.

        Raises:
            ProviderError: On a provider failure, an empty reply, or a reply that does not
                register the expected tool.
        """
        if not description.strip():
            raise ProviderError("describe what the tool should do first")
        raw = self._llm.generate_text(
            build_user_tool_message(slug, description, field_names),
            system=user_tool_system_prompt(),
            temperature=envs.OMNIA_SMART_NOTES_TOOL_AUTHOR_TEMPERATURE,
        )
        return parse_user_tool_reply(raw, slug)
