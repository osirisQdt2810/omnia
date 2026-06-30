"""Data models for prompt authoring (auto-smart suggestions).

Pure module — no Anki imports.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AutoSmartDep:
    """The LLM's suggested dependency of a field onto a prerequisite ``field``.

    ``kind`` is ``"hard"`` (the prerequisite's content is required to generate the field) or
    ``"soft"`` (helpful optional context). Maps onto the config's ``FieldDep`` when applied.
    """

    field: str
    kind: str


@dataclass(frozen=True)
class AutoSmartField:
    """The LLM's suggestion for one field: its generation ``type`` + ``prompt`` template.

    ``depends_on`` carries the model's proposed dependency edges onto other fields (empty when
    the model returned none).
    """

    type: str
    prompt: str
    depends_on: tuple[AutoSmartDep, ...] = ()


@dataclass(frozen=True)
class EdgeKinding:
    """The dependency classifier's verdict for ONE referenced ``field``.

    ``kind`` is ``"hard"`` (the dependent's content fundamentally is-about / cannot exist
    without ``field``) or ``"soft"`` (``field`` only sharpens an output already producible
    without it). ``reason`` is the model's optional one-line justification (kept for surfacing
    in the UI / logs; not used by the reconciler).
    """

    field: str
    kind: str
    reason: str = ""


@dataclass(frozen=True)
class EdgeChange:
    """One graph→prompt edge edit the user made, to be reflected in a field's prompt.

    ``action`` is ``"add"`` (a new dependency on ``src``), ``"remove"`` (drop the dependency on
    ``src``), or ``"toggle"`` (the dependency on ``src`` flipped between hard/soft). ``src`` is
    the referenced (prerequisite) field; ``old_kind``/``new_kind`` carry the kinds involved
    (e.g. a toggle from ``hard`` to ``soft``; an add carries only ``new_kind``).
    """

    action: str
    src: str
    old_kind: str = ""
    new_kind: str = ""


@dataclass(frozen=True)
class PromptRewrite:
    """The outcome of a guard-railed prompt rewrite (graph→prompt or pinned improve).

    ``prompt`` is the prompt to persist: the validated rewrite when ``ok`` is True, otherwise
    the unchanged ``old_prompt`` (the rewrite failed the consistency gate). ``ok`` reports
    whether the rewrite derived the INTENDED dependency edge set; ``reason`` explains a failure
    (empty on success).
    """

    prompt: str
    ok: bool
    old_prompt: str = ""
    reason: str = ""
