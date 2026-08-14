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
from omnia.plugins.smart_notes.engine.tools.user_tools import (
    ImportGuard,
    user_tool_name,
)

if TYPE_CHECKING:
    from omnia.core.providers.llm.base import LLMProvider

# A ```python fenced block, if the model wrapped its answer in one.
_FENCE_RE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL)

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
    return (
        "You are a senior Python engineer writing ONE plugin file for the Anki add-on Omnia. "
        "The file defines a deterministic 'tool' that fills a single note field by "
        "TRANSFORMING other fields of the same note. It never calls an AI model.\n\n"
        "Output rules (all mandatory):\n"
        "1. Output ONLY the Python module. No prose, no explanation, no code fences.\n"
        "2. Define EXACTLY ONE Tool subclass, decorated @register_tool with the exact name "
        "you are given, and set the same string as its `name` ClassVar.\n"
        "3. Declare the ClassVars: name, label (2-3 words), description (one sentence), "
        'kinds = frozenset({"text"}), deterministic = True, and params_model.\n'
        "4. params_model is a pydantic model deriving from omnia.core.config.base."
        "PersistedModel, with a default for EVERY option (the tool must work with no params "
        "configured). Give each option a Field(description=...) — the settings UI renders the "
        "form from that schema. Name any option that holds a NOTE FIELD NAME `<something>_field`"
        " and return those names from `referenced_fields` so the dependency graph sees them.\n"
        "5. `run(self, request, ctx)` returns Produced(GenerationResult('text', text=...)) on "
        "success, NotApplicable(reason) when a precondition is unmet (an empty source field), "
        "or Empty(reason) when the transform ran and found nothing. Both non-produced outcomes "
        "let the next tool in the field's chain try, so prefer them over raising.\n"
        "6. NEVER call an LLM/TTS provider, read or write files, open a network connection, "
        "or touch the Anki collection. `ctx` is available but a deterministic tool ignores it. "
        f"You may ONLY import from: {allowed}.\n"
        "7. Pure standard-library string work. Keep it short, readable and commented where the "
        "logic is not obvious — a human reads this file before it is allowed to run.\n\n"
        "This is a complete, correct example of the required shape:\n\n"
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
        f"For context, one note type using this tool has the fields: {fields}. "
        "Do NOT hard-code them — the tool must work wherever its params point.\n\n"
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
    match = _FENCE_RE.search(raw or "")
    code = (match.group(1) if match else (raw or "")).strip()
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
