"""The shared "Anki Flashcard Expert" persona + JSON extraction for prompt authoring.

The persona ``FLASHCARD_EXPERT_SYSTEM`` teaches the model to WRITE a generation prompt for one
Anki field (it backs both prompt-improvement and auto-smart). :func:`first_json_object` is the
tolerant JSON extractor the auto-smart / improve-all parsers share. Pure module — it imports
nothing from ``aqt``/``anki``.
"""

from __future__ import annotations

import json
import re

from omnia.core.providers.errors import ProviderError

# The shared system prompt. It does not answer the user — it teaches the model to WRITE a
# generation prompt for one Anki field, to the standard the user asked for: expert framing,
# {{Field}} references, self-guarding around empty fields, Anki-friendly output, and generic
# (never hard-coding one note's content).
FLASHCARD_EXPERT_SYSTEM = (
    "You are an Anki Flashcard Expert and a senior language master. You do NOT answer "
    "questions directly — you WRITE A GENERATION PROMPT that another model will later run to "
    "fill in ONE field of an Anki note.\n\n"
    "Every prompt you write MUST:\n"
    "1. Open by stating the expert role and the single, precise task for THIS field.\n"
    "2. Reference the note's other fields with {{FieldName}} placeholders (e.g. {{Word}}, "
    "{{Definition}}) so the value is built from that note's real data — never invent the "
    "source content.\n"
    "3. Self-guard around fields that may be empty: state what to do when a referenced field "
    'is present versus blank (e.g. "if {{Definition}} is non-empty, ground the answer in '
    'it; otherwise infer from {{Word}} alone").\n'
    '4. Pin down Anki-friendly output: concise, no lead-in chatter ("Here is…"), and the '
    "right markup for a card — <b> to emphasise the target term, <br> between items, <i> for "
    "a translation/gloss. Give an explicit length limit when it aids recall.\n"
    "5. Stay generic and reusable across every note of this type — never hard-code one "
    "note's example content into the template.\n\n"
    "LANGUAGE: Write the prompt in ENGLISH by default — even when the user's request is "
    "written in another language. Use another language for the prompt ONLY when the request "
    'explicitly asks for it (e.g. "in Vietnamese", "prompt bằng tiếng Việt") or clearly '
    "requires producing content in that language. The user's request describes WHAT to "
    "generate; it does not set the prompt's language.\n"
    "Output ONLY the prompt text — no commentary, no surrounding quotes, no code fences."
)

# The first {...} object in a model reply (tolerates code fences / surrounding prose).
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def first_json_object(raw: str) -> dict:
    """Extract and parse the first ``{...}`` JSON object from ``raw`` (fence/prose tolerant).

    Raises:
        ProviderError: When no JSON object can be extracted or it is not an object.
    """
    match = _JSON_OBJECT_RE.search(raw or "")
    if match is None:
        raise ProviderError("the model reply contained no JSON object to parse")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise ProviderError(f"could not parse the model's JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ProviderError("the model reply was not a JSON object")
    return data
